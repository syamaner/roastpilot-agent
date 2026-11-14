"""Synthetic compatibility and coverage contracts for D230 S2b."""

import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pydantic
import pytest

import roastpilot_agent.diagnostic_collector as collector
from roastpilot_agent.diagnostic_collector import (
    CollectorAdmissionError,
    CollectorAdmissionReason,
    CollectorAuthority,
    CollectorStream,
    InterpretationInputError,
    InterpretationInputReason,
    SyntheticExpectations,
    bundle_reader,
    coverage_schema,
    interpret_synthetic_bundle,
)
from roastpilot_agent.diagnostic_collector._clock_projection import ClockProjection
from roastpilot_agent.diagnostic_collector.bundle_schema import AlignmentAnchor, ClockName, Instant
from roastpilot_agent.diagnostic_collector.coverage_interpreter import interpret_coverage_bundle
from roastpilot_agent.diagnostic_collector.coverage_schema import (
    COVERAGE_REASON_STATUS,
    CoverageCheckName,
    CoverageCheckResult,
    CoverageExpectations,
    CoverageReason,
    CoverageReport,
    DetectorLatticeBinding,
    DetectorSampleAnchor,
    LatticeCoverageDetail,
    RecordingBinding,
    RecordingExtentDetail,
)
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    MAX_COORDINATE,
    CheckReason,
    CheckStatus,
    ClockScale,
    ProvenanceClass,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "d230_collector" / "synthetic-v1.jsonl"
_A = ClockName.AGENT_MONOTONIC_SECONDS
_M = ClockName.MCP_SESSION_SECONDS
_S = ClockName.RECORDING_SAMPLES
_U = ClockName.UTC_EPOCH_SECONDS

_Point = tuple[ClockName, int]
_Anchor = tuple[_Point, _Point]
_ClockCase = tuple[str, _Point, _Point, tuple[_Anchor, ...], tuple[ClockScale, ...], int | None]


def _scale(
    source: ClockName, target: ClockName, factor: int = 1, *, end: int = MAX_COORDINATE
) -> ClockScale:
    return ClockScale(
        source_clock=source,
        target_clock=target,
        target_per_source=factor,
        valid_from=0,
        valid_to=end,
    )


def _mixed_clock_scenarios() -> Iterator[_ClockCase]:
    baseline = (_A, 10)
    reverse = (((_A, 5), (_M, 10)),)
    alternatives = (((_U, 0), (_A, 0)), ((_U, 0), (_M, 0)), ((_M, 0), (_A, 0)))
    yield "absent-rate", (_S, 200), baseline, (((_S, 100), (_A, 7)),), (), None
    yield "same-clock-rate", (_S, 100), (_S, 125), (), (), 100
    yield "same-clock-no-rate", (_S, 100), (_S, 125), (), (), None
    yield "forward-valid", (_A, 6), (_M, 14), reverse, (_scale(_A, _M, 2, end=6),), 100
    yield "reverse-valid", (_M, 12), baseline, reverse, (_scale(_A, _M, 2, end=6),), 100
    yield "reverse-expired", (_M, 13), baseline, reverse, (_scale(_A, _M, 2, end=6),), 100
    yield "raw-out-of-domain", (_A, MAX_COORDINATE + 1), baseline, (), (), 100
    yield (
        "intermediate-out-of-domain",
        (_U, 1),
        baseline,
        (((_U, 0), (_A, 0)), ((_U, 0), (_S, MAX_COORDINATE)), ((_S, MAX_COORDINATE), (_A, 0))),
        (_scale(_U, _A),),
        1,
    )
    yield (
        "conflicting-paths",
        (_U, 3),
        baseline,
        alternatives,
        (_scale(_U, _A), _scale(_U, _M), _scale(_M, _A, 2)),
        100,
    )
    yield (
        "expired-path-does-not-vote",
        (_U, 3),
        baseline,
        alternatives,
        (_scale(_U, _A), _scale(_U, _M), _scale(_M, _A, 2, end=2)),
        100,
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _serialized_s2a(
    reading: _Point,
    reference: _Point,
    anchors: tuple[_Anchor, ...],
    scales: tuple[ClockScale, ...],
    rate: int | None,
) -> bytes:
    header, *rows = [json.loads(line) for line in _FIXTURE.read_bytes().splitlines()]
    header["alignments"] = [
        {
            "source_clock": source.value,
            "source_value": source_value,
            "target_clock": target.value,
            "target_value": target_value,
        }
        for (source, source_value), (target, target_value) in anchors
    ]
    for row in rows:
        if row["stream"] == "temperature_turn":
            row["payload"]["observed_at"] = {"clock": reading[0].value, "value": reading[1]}
    data = b"".join(_canonical_bytes(row) + b"\n" for row in (header, *rows))
    return (
        interpret_synthetic_bundle(
            data,
            expected_sha256=hashlib.sha256(data).hexdigest(),
            expectations=SyntheticExpectations(
                session_id="session_1",
                bundle_id="bundle_1",
                evaluation_at=Instant(clock=reference[0], value=reference[1]),
                max_temperature_age_seconds=10,
                recording_sample_rate_hz=rate,
                clock_scales=scales,
            ),
        )
        .model_dump_json()
        .encode()
    )


# Captured against e14af837, before moving the clock projection implementation.
_S2A_CLOCK_DIGESTS = {
    "absent-rate": "0b48358ec5967d60c620549407d0c6cd59f8175bd3801184c5560cebcc232ea7",
    "same-clock-rate": "9517de5a2664f506d666c875102172fae48bc72a8dbef43badf8f3310a56f399",
    "same-clock-no-rate": "195160c5c99789f89e2627ff7012fa6a22d728e9ce49743937f7b90a7c38ef1b",
    "forward-valid": "e0ce08b5dccc9624ede972c60eb6558c62de9ce9cff169f372f96b18f713523f",
    "reverse-valid": "5039bd749b8f8a257c55b156a9c49e6d380db24bd480d6c5d8526f675b180d31",
    "reverse-expired": "7b0ec59992808c61c4b7d5132a2ef46c686db008cff34a0e1679b6eccce0c6c9",
    "raw-out-of-domain": "0b3ef1ececd8aa3c4bc609b93bedb8e894380ee4407c8f137276f4b3ea6385df",
    "intermediate-out-of-domain": (
        "5f6f2e751fb132e2db1d3c5705d443f7158c6e9d43bbef10d400ad8c57c09559"
    ),
    "conflicting-paths": "b17f152a38a4d840d8dfdfc4389d29ccf2ad96643823cc5db07e9b04f9f3bcc3",
    "expired-path-does-not-vote": (
        "f9f1c7904b010365931b2538b36f954870859c1bd2ed0fe1dd90b9a876e623e1"
    ),
}


def test_g1_s2a_report_and_exports_are_unchanged() -> None:
    """Pin every report field for the raw fixture and the public package API."""
    data = _FIXTURE.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == "2e316e08c29766a2427aa87949cd8eb97e3abf20d06dcb700892a23effbee4d8"
    report = interpret_synthetic_bundle(
        data,
        expected_sha256=digest,
        expectations=SyntheticExpectations(session_id="session_1", bundle_id="bundle_1"),
    )
    serialized = _canonical_bytes(report.model_dump(mode="json"))
    assert (
        hashlib.sha256(serialized).hexdigest()
        == "6224db9e67c0b7c8fe65553acff4fe53c54e39bf2723fc3bebbccf6279c1c61b"
    )
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


@pytest.mark.parametrize(
    ("name", "reading", "reference", "anchors", "scales", "rate"),
    [pytest.param(*case, id=case[0]) for case in _mixed_clock_scenarios()],
)
def test_g2_s2a_clock_report_bytes_are_unchanged(
    name: str,
    reading: _Point,
    reference: _Point,
    anchors: tuple[_Anchor, ...],
    scales: tuple[ClockScale, ...],
    rate: int | None,
) -> None:
    """Pin exact serialized output across bounded and conflicting clock paths."""
    report = _serialized_s2a(reading, reference, anchors, scales, rate)
    assert hashlib.sha256(report).hexdigest() == _S2A_CLOCK_DIGESTS[name]


@pytest.mark.parametrize(
    ("name", "reading", "target", "anchors", "scales", "rate", "expected"),
    [
        pytest.param(*case[:2], case[2][0], *case[3:], expected, id=case[0])
        for case, expected in zip(
            _mixed_clock_scenarios(),
            (
                CheckReason.CLOCK_ALIGNMENT_UNRESOLVED,
                100,
                100,
                12,
                6,
                CheckReason.CLOCK_ALIGNMENT_UNRESOLVED,
                CheckReason.INSTANT_OUT_OF_DOMAIN,
                CheckReason.INSTANT_OUT_OF_DOMAIN,
                CheckReason.CLOCK_PATH_CONFLICT,
                3,
            ),
            strict=True,
        )
    ],
)
def test_p1_exact_clock_projection(
    name: str,
    reading: _Point,
    target: ClockName,
    anchors: tuple[_Anchor, ...],
    scales: tuple[ClockScale, ...],
    rate: int | None,
    expected: CheckReason | int,
) -> None:
    """Exercise the shared projection directly across all clock guard families."""
    projection = ClockProjection(
        tuple(
            AlignmentAnchor(
                source_clock=source[0],
                source_value=source[1],
                target_clock=end[0],
                target_value=end[1],
            )
            for source, end in anchors
        ),
        scales,
        rate,
    )
    assert projection.rate == rate
    assert projection.point(Instant(clock=reading[0], value=reading[1]), target) == expected, name


def test_p1_does_not_default_an_unscaled_seconds_mapping() -> None:
    """An anchor without a seconds scale supplies no seconds-to-seconds path."""
    anchor = AlignmentAnchor(source_clock=_A, source_value=0, target_clock=_M, target_value=0)
    projector = ClockProjection((anchor,), (), 100)
    actual = projector.point(Instant(clock=_A, value=1), _M)
    assert actual is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED


def _detector(**changes: object) -> DetectorLatticeBinding:
    fields = dict(
        sample_rate_hz=16000,
        origin_sample=0,
        charge_sample=0,
        drop_sample=30000,
        hop_samples=5333,
        length_samples=8000,
    )
    return DetectorLatticeBinding.model_validate(fields | changes)


def _recording(**changes: object) -> RecordingBinding:
    fields = dict(
        recording_id="rec_a",
        sample_rate_hz=16000,
        required_first_sample=0,
        required_end_sample=1000,
    )
    return RecordingBinding.model_validate(fields | changes)


def _expect(**changes: object) -> CoverageExpectations:
    fields: dict[str, object] = {
        "session_id": "session_1",
        "bundle_id": "bundle_1",
        "detector": _detector(),
        "recording": _recording(),
    }
    return CoverageExpectations.model_validate(fields | changes)


def _at(value: int | float, clock: ClockName = _S) -> dict[str, object]:
    return {"clock": clock.value, "value": value}


def _window(start: int, **changes: object) -> dict[str, object]:
    end = start + 8000
    row: dict[str, object] = {
        "start_at": _at(start),
        "end_at": _at(end),
        "decision_at": _at(end),
        "eligibility": "eligible",
        "status": "negative",
        "probability": 0,
    }
    return row | changes


def _skip(start: int, eligibility: str = "post_drop") -> dict[str, object]:
    return _window(
        start, eligibility=eligibility, status="not_inferred", probability=None, decision_at=None
    )


def _segment(
    first: int, end: int, *, recording_id: str = "rec_a", rate: int = 16000
) -> dict[str, object]:
    return {
        "recording_id": recording_id,
        "first_sample": first,
        "sample_count": end - first,
        "sample_rate_hz": rate,
    }


_PAYLOADS = dict[str, list[dict[str, object]]]


def _data(
    windows: list[dict[str, object]] | None = None,
    segments: list[dict[str, object]] | None = None,
    *,
    other: _PAYLOADS | None = None,
    anchors: tuple[_Anchor, ...] = (),
    session: str = "session_1",
) -> bytes:
    streams = (other or {}) | {
        "shadow_window": windows if windows is not None else [_window(n * 5333) for n in range(5)],
        "recording_segment": segments if segments is not None else [_segment(0, 1000)],
    }
    header = {
        "alignments": [
            {
                "source_clock": source[0].value,
                "source_value": source[1],
                "target_clock": target[0].value,
                "target_value": target[1],
            }
            for source, target in anchors
        ],
        "bundle_id": "bundle_1",
        "evidence_class": "synthetic",
        "inventory": {item.value: len(streams.get(item.value, ())) for item in CollectorStream},
        "schema_version": 1,
        "session_id": session,
    }
    rows = (
        {
            "schema_version": 1,
            "session_id": session,
            "stream": stream,
            "seq": index,
            "payload": payload,
        }
        for stream, payloads in streams.items()
        for index, payload in enumerate(payloads)
    )
    return b"".join(_canonical_bytes(row) + b"\n" for row in (header, *rows))


def _coverage(data: bytes, expectations: CoverageExpectations | None = None) -> CoverageReport:
    return interpret_coverage_bundle(
        data,
        expected_sha256=hashlib.sha256(data).hexdigest(),
        expectations=expectations or _expect(),
    )


def _outcome(report: CoverageReport, check: CoverageCheckName) -> CoverageCheckResult:
    return next(result for result in report.checks if result.check is check)


def _lattice(report: CoverageReport) -> CoverageCheckResult:
    return _outcome(report, CoverageCheckName.LATTICE_SCORE_COVERAGE)


def _extent(report: CoverageReport) -> CoverageCheckResult:
    return _outcome(report, CoverageCheckName.RECORDING_EXTENT_COVERAGE)


@pytest.mark.parametrize("value", [bytearray(b"{}"), memoryview(b"{}")])
def test_b1_nonbytes_refused_before_admission(
    value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interpreter admits original bytes only."""
    data = _data()
    admitted = bundle_reader.read_synthetic_bundle(
        data, expected_sha256=hashlib.sha256(data).hexdigest()
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("admission called before validating input type")

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", unexpected)
    for invalid in (value, admitted):
        with pytest.raises(InterpretationInputError) as caught:
            interpret_coverage_bundle(
                cast(bytes, invalid), expected_sha256="x", expectations=_expect()
            )
        assert caught.value.reason is InterpretationInputReason.DATA_NOT_BYTES
        assert str(caught.value) == "synthetic collector interpretation input refused"


@pytest.mark.parametrize(
    "field, bad",
    [
        ("charge_sample", 30000),
        ("hop_samples", 0),
        ("sample_rate_hz", True),
        ("drop_sample", MAX_COORDINATE + 1),
        (
            "anchor",
            DetectorSampleAnchor.model_construct(seconds_clock=_S, seconds_value=0, sample_value=0),
        ),
        (
            "anchor",
            DetectorSampleAnchor.model_construct(
                seconds_clock=_A, seconds_value=True, sample_value=0
            ),
        ),
    ],
)
def test_b1_rebuilds_invalid_nested_bindings(
    field: str, bad: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructed nested models cannot bypass strict expectation validation."""
    raw = vars(_detector()) | {field: bad}
    detector = DetectorLatticeBinding.model_construct(_fields_set=None, **raw)
    expect = CoverageExpectations.model_construct(
        session_id="session_1", bundle_id="bundle_1", detector=detector
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("admission called before validating expectations")

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", unexpected)
    with pytest.raises(InterpretationInputError) as caught:
        interpret_coverage_bundle(b"", expected_sha256="x", expectations=expect)
    assert caught.value.reason is InterpretationInputReason.EXPECTATIONS_INVALID
    assert str(caught.value) == "synthetic collector interpretation input refused"


def test_b1_rejects_invalid_recording_and_preserves_optional_nulls() -> None:
    """Recording bounds fail closed and explicit absence remains distinguishable from zero."""
    invalid = RecordingBinding.model_construct(
        recording_id="rec_a", sample_rate_hz=16000, required_first_sample=0, required_end_sample=0
    )
    expectations = CoverageExpectations.model_construct(
        session_id="session_1", bundle_id="bundle_1", recording=invalid
    )
    with pytest.raises(InterpretationInputError) as caught:
        interpret_coverage_bundle(b"", expected_sha256="x", expectations=expectations)
    assert caught.value.reason is InterpretationInputReason.EXPECTATIONS_INVALID
    fields = {"session_id": "session_1", "bundle_id": "bundle_1"}
    assert CoverageExpectations.model_validate(fields) == CoverageExpectations.model_validate(
        fields | {"detector": None, "recording": None}
    )
    with pytest.raises(pydantic.ValidationError):
        CoverageExpectations.model_validate(fields | {"clock_scales": None})
    with pytest.raises(pydantic.ValidationError):
        _expect(clock_scales=(_scale(_A, _M), _scale(_M, _A)))
    with pytest.raises(pydantic.ValidationError):
        CoverageExpectations.model_validate(fields | {"session_id": 42})


def test_b2_calls_admission_once_preserving_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    """One call forwards the exact data and digest and does not swallow reader errors."""
    data = _data()
    digest = hashlib.sha256(data).hexdigest()
    real = bundle_reader.read_synthetic_bundle
    refused = CollectorAdmissionError(CollectorAdmissionReason.NON_CANONICAL)
    calls: list[tuple[bytes, str]] = []

    def spy(source: bytes, *, expected_sha256: str) -> collector.AdmittedBundle:
        calls.append((source, expected_sha256))
        return real(source, expected_sha256=expected_sha256)

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", spy)
    report = interpret_coverage_bundle(data, expected_sha256=digest, expectations=_expect())
    assert len(calls) == 1 and calls[0][0] is data and calls[0][1] is digest
    assert report.bundle_sha256 == digest

    def fail(_source: bytes, *, expected_sha256: str) -> collector.AdmittedBundle:
        assert expected_sha256 is digest
        raise refused

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", fail)
    with pytest.raises(CollectorAdmissionError) as caught:
        interpret_coverage_bundle(data, expected_sha256=digest, expectations=_expect())
    assert caught.value is refused


@pytest.mark.parametrize("reason", list(CoverageReason))
def test_b3_report_rejects_reason_status_mismatches(reason: CoverageReason) -> None:
    """Each reason maps to exactly one non-satisfied status."""
    for status in CheckStatus:
        if status is COVERAGE_REASON_STATUS[reason]:
            continue
        with pytest.raises(pydantic.ValidationError, match="reason does not match"):
            CoverageCheckResult(
                check=CoverageCheckName.OBSERVED_PROVENANCE,
                reason=reason,
                status=status,
                evidence=(),
                detail=None,
            )


def test_b3_report_validates_detail_and_inventory() -> None:
    """Closed results cannot lose checks, limits or required detail."""
    report = _coverage(_data())
    expected_order = tuple(CoverageCheckName)
    assert tuple(result.check for result in report.checks) == expected_order
    assert report.report_schema_version == 1
    assert report.report_name.value == "coverage"
    assert report.authority is CollectorAuthority.NONE
    assert report.provenance is ProvenanceClass.SYNTHETIC_UNATTESTED
    provenance = report.checks[-1]
    assert provenance.reason is CoverageReason.SYNTHETIC_EVIDENCE and provenance.evidence == ()
    for changes in (
        {"checks": report.checks[1:]},
        {"checks": tuple(reversed(report.checks))},
        {"adapter_limits": report.adapter_limits[:-1]},
    ):
        with pytest.raises(pydantic.ValidationError):
            CoverageReport.model_validate(vars(report) | changes)
    lattice, _, extent, _, _ = report.checks
    assert lattice.detail is not None and extent.detail is not None
    for result, changes in (
        (lattice, {"detail": extent.detail}),
        (lattice, {"detail": None}),
        (provenance, {"detail": lattice.detail}),
        (lattice, {"reason": CoverageReason.EMPTY_DENOMINATOR, "status": CheckStatus.UNRESOLVED}),
    ):
        with pytest.raises(pydantic.ValidationError):
            CoverageCheckResult.model_validate(vars(result) | changes)
    for changes in ({"scored_slots": 4}, {"errored_slots": 1}, {"duplicate_rows": True}):
        with pytest.raises(pydantic.ValidationError):
            LatticeCoverageDetail.model_validate(vars(lattice.detail) | changes)
    with pytest.raises(pydantic.ValidationError):
        RecordingExtentDetail.model_validate(vars(extent.detail) | {"uncovered_samples": 1})


@pytest.mark.parametrize(
    "module, plain, collector_modules, classes, functions",
    [
        (
            "_clock_projection",
            {"fractions", "typing"},
            {"bundle_schema", "interpretation_schema"},
            {"ClockProjection"},
            set[str](),
        ),
        (
            "coverage_schema",
            {"enum", "typing", "pydantic"},
            {"bundle_schema", "interpretation_schema"},
            {
                "CoverageExpectations",
                "DetectorLatticeBinding",
                "DetectorSampleAnchor",
                "RecordingBinding",
                "CoverageCheckName",
                "CoverageReason",
                "CoverageAdapterLimit",
                "CoverageReportName",
                "LatticeCoverageDetail",
                "RecordingExtentDetail",
                "CoverageCheckResult",
                "CoverageReport",
            },
            set[str](),
        ),
        (
            "coverage_interpreter",
            {"fractions", "typing", "pydantic"},
            {
                "_clock_projection",
                "bundle_schema",
                "bundle_reader",
                "coverage_schema",
                "interpretation_schema",
            },
            set[str](),
            {"interpret_coverage_bundle"},
        ),
    ],
)
def test_p2_b4_pure_module_boundaries(
    module: str,
    plain: set[str],
    collector_modules: set[str],
    classes: set[str],
    functions: set[str],
) -> None:
    """Pure evaluators inherit no runtime, cross-interpreter or unbounded range dependency."""
    prefix = "roastpilot_agent.diagnostic_collector."
    allowed = plain | {prefix + allowed for allowed in collector_modules}
    path = Path(coverage_schema.__file__).with_name(module + ".py")
    tree = ast.parse(path.read_bytes())
    forbidden = {"open", "eval", "exec", "__import__"}
    if module == "coverage_interpreter":
        forbidden.add("range")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
            assert all(alias.name != "SyntheticExpectations" for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            assert name not in forbidden
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
    if module == "_clock_projection":
        assert "SyntheticExpectations" not in ast.unparse(tree)


def test_b4_fresh_process_has_no_control_imports() -> None:
    """Transitive imports from the coverage entrypoint stay offline and control-free."""
    code = """
import sys
import roastpilot_agent.diagnostic_collector.coverage_interpreter
blocked = {'controller', 'safety', 'mcp_client', 'live', 'mcp'}
assert blocked.isdisjoint(name.rsplit('.', 1)[-1] for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
    )
    assert result.returncode == 0, result.stderr


def _lattice_counts(result: CoverageCheckResult) -> LatticeCoverageDetail:
    assert isinstance(result.detail, LatticeCoverageDetail)
    return result.detail


def _extent_counts(result: CoverageCheckResult) -> RecordingExtentDetail:
    assert isinstance(result.detail, RecordingExtentDetail)
    return result.detail


def test_l1_and_l10_model_scores_leave_capture_continuity_unresolved() -> None:
    """Five explicitly scored slots satisfy score coverage only."""
    report = _coverage(_data())
    result = _lattice(report)
    counts = _lattice_counts(result)
    assert result.status is CheckStatus.SATISFIED
    assert (counts.expected_slots, counts.scored_slots, counts.missed_slots) == (5, 5, 0)
    assert [ref.seq for ref in result.evidence] == list(range(5))
    assert result.evidence[0].instants == (
        Instant(clock=_S, value=0),
        Instant(clock=_S, value=8000),
        Instant(clock=_S, value=8000),
    )
    continuity = _outcome(report, CoverageCheckName.DETECTOR_CAPTURE_CONTINUITY)
    assert continuity.reason is CoverageReason.PRODUCER_WITNESS_ABSENT
    assert continuity.status is CheckStatus.UNRESOLVED and continuity.evidence == result.evidence


@pytest.mark.parametrize("origin", [0, 5333])
def test_l2_consecutive_starts_cannot_prove_a_complete_interval(origin: int) -> None:
    """Either end of a fixed lattice can go missing despite consecutive evidence."""
    data = _data([_window(origin + index * 5333) for index in range(4)])
    result = _lattice(_coverage(data))
    assert result.reason is CoverageReason.SLOTS_MISSED
    assert _lattice_counts(result).missed_slots == 1
    if origin:
        shifted = _lattice(_coverage(data, _expect(detector=_detector(origin_sample=origin))))
        assert shifted.status is CheckStatus.SATISFIED
        assert _lattice_counts(shifted).expected_slots == 4
    assert (
        _lattice(_coverage(data, _expect(detector=None))).reason
        is CoverageReason.EXPECTATION_UNBOUND
    )


def test_l4_charge_inclusive_and_drop_strict() -> None:
    """Exact charge passes and exact drop is excluded by the independent denominator."""
    rows = [_window(index * 5333) for index in range(4)]
    expectations = _expect(detector=_detector(drop_sample=29332))
    exact = _lattice(_coverage(_data([*rows, _skip(21332)]), expectations))
    assert exact.status is CheckStatus.SATISFIED
    assert _lattice_counts(exact).expected_slots == 4
    assert _lattice_counts(exact).ineligible_rows == 1
    disputed = _lattice(_coverage(_data([*rows, _window(21332)]), expectations))
    assert disputed.reason is CoverageReason.CONFLICT
    trailing = _lattice(_coverage(_data([*rows, _window(21332), _skip(26665)])))
    assert trailing.status is CheckStatus.SATISFIED
    assert _lattice_counts(trailing).expected_slots == 5
    assert _lattice_counts(trailing).ineligible_rows == 1


def test_l5_post_drop_precedes_pre_charge_and_empty_denominator() -> None:
    """A valid straddle cannot certify coverage, and a wrong label is a conflict."""
    expectations = _expect(detector=_detector(charge_sample=1000, drop_sample=7000))
    wrong = _lattice(_coverage(_data([_skip(0, "pre_charge")]), expectations))
    correct = _lattice(_coverage(_data([_skip(0)]), expectations))
    absent = _lattice(_coverage(_data([]), expectations))
    assert wrong.reason is CoverageReason.CONFLICT and wrong.detail is None
    assert correct.reason is CoverageReason.EMPTY_DENOMINATOR
    assert _lattice_counts(correct).expected_slots == 0
    assert absent.reason is CoverageReason.EVIDENCE_ABSENT and absent.detail is None


@pytest.mark.parametrize(
    "rows, reason",
    [
        ([_window(0), _window(10666), _window(15999), _window(21332)], CoverageReason.SLOTS_MISSED),
        ([_window(1)], CoverageReason.OFF_LATTICE),
        ([_window(0, end_at=_at(7999))], CoverageReason.OFF_LATTICE),
        ([_window(0, decision_at=_at(7999))], CoverageReason.ORDER_CONFLICT),
        ([_window(0, probability=None)], CoverageReason.EVIDENCE_MALFORMED),
        ([_window(0), _window(0, probability=0.5)], CoverageReason.CONFLICT),
        ([_window(0), _window(0, status="error", probability=None)], CoverageReason.CONFLICT),
    ],
)
def test_l6_l7_lattice_failures(rows: list[dict[str, object]], reason: CoverageReason) -> None:
    """Keep cadence, score admission, chronology and repeated-row conflicts distinct."""
    result = _lattice(_coverage(_data(rows)))
    assert result.reason is reason
    assert (result.detail is not None) is (reason is CoverageReason.SLOTS_MISSED)


def test_l6_l7_failed_slots_and_equal_duplicates_have_distinct_counts() -> None:
    """An eligible error counts as missed and a repeated score counts once."""
    rows = [_window(index * 5333) for index in range(5)]
    duplicated = _lattice(_coverage(_data([*rows, rows[0]])))
    assert duplicated.status is CheckStatus.SATISFIED
    assert _lattice_counts(duplicated).duplicate_rows == 1
    error = _window(0, status="error", probability=None)
    result = _lattice(_coverage(_data([error, *rows[1:], _skip(26665)])))
    counts = _lattice_counts(result)
    assert result.reason is CoverageReason.SLOTS_MISSED
    assert (counts.missed_slots, counts.errored_slots, counts.ineligible_rows) == (1, 1, 1)


def _clock_case(**changes: object) -> DetectorLatticeBinding:
    fields: dict[str, object] = {
        "sample_rate_hz": 4,
        "hop_samples": 4,
        "length_samples": 4,
        "drop_sample": 5,
        "anchor": DetectorSampleAnchor(seconds_clock=_A, seconds_value=0, sample_value=0),
    }
    return _detector(**(fields | changes))


def _seconds_window(*, start: float = 0, decision: float = 1.125) -> dict[str, object]:
    return _window(0, start_at=_at(start, _A), end_at=_at(1, _A), decision_at=_at(decision, _A))


def test_l8_detector_clock_uses_bound_anchor_and_allows_fractional_decision() -> None:
    """A sample anchor is independently required for seconds and only boundaries are integral."""
    report = _lattice(_coverage(_data([_seconds_window()]), _expect(detector=_clock_case())))
    assert report.status is CheckStatus.SATISFIED
    assert _lattice_counts(report).scored_slots == 1
    wrong_source = _data([_seconds_window()], anchors=(((_A, 0), (_S, 0)),))
    no_binding = _expect(detector=_clock_case(anchor=None))
    assert (
        _lattice(_coverage(wrong_source, no_binding)).reason
        is CoverageReason.CLOCK_ALIGNMENT_UNRESOLVED
    )
    assert (
        _lattice(_coverage(_data([_seconds_window()]), no_binding)).reason
        is CoverageReason.CLOCK_ALIGNMENT_UNRESOLVED
    )
    fractional_start = _data([_seconds_window(start=0.125)])
    assert (
        _lattice(_coverage(fractional_start, _expect(detector=_clock_case()))).reason
        is CoverageReason.OFF_LATTICE
    )


def test_l8_disagreeing_seconds_paths_stay_unresolved() -> None:
    """A valid direct clock route cannot outvote a contradictory indirect route."""
    anchors = (((_U, 0), (_A, 0)), ((_U, 0), (_M, 0)), ((_M, 0), (_A, 0)))
    scales = (_scale(_U, _A), _scale(_U, _M), _scale(_M, _A, 2))
    row = _seconds_window() | {"end_at": _at(1, _U)}
    result = _lattice(
        _coverage(
            _data([row], anchors=anchors), _expect(detector=_clock_case(), clock_scales=scales)
        )
    )
    assert result.reason is CoverageReason.CLOCK_PATH_CONFLICT
    assert result.status is CheckStatus.UNRESOLVED and result.detail is None


def test_l9_huge_denominator_does_not_construct_slots() -> None:
    """The expected count is computed arithmetically beyond any row budget."""
    expectation = _expect(
        detector=_detector(drop_sample=MAX_COORDINATE, hop_samples=1, length_samples=1)
    )
    rows = [_window(index, end_at=_at(index + 1), decision_at=_at(index + 1)) for index in range(3)]
    result = _lattice(_coverage(_data(rows), expectation))
    assert result.reason is CoverageReason.SLOTS_MISSED
    counts = _lattice_counts(result)
    assert counts.expected_slots == 2**53 - 2
    assert counts.scored_slots == 3 and counts.missed_slots == 2**53 - 5


@pytest.mark.parametrize(
    "segments, reason, detail",
    [
        ([_segment(0, 500), _segment(500, 1000)], None, {"covered_samples": 1000}),
        ([_segment(0, 499), _segment(500, 1000)], CoverageReason.GAP, {"uncovered_samples": 1}),
        ([_segment(0, 900)], CoverageReason.GAP, {"uncovered_samples": 100}),
        ([_segment(0, 1000)] * 2, None, {"exact_duplicate_rows": 1}),
        (
            [_segment(0, 1000), _segment(100, 200)],
            CoverageReason.CONFLICT,
            {"contained_spans": 1, "overlapping_spans": 2},
        ),
        (
            [_segment(0, 1000), _segment(0, 200)],
            CoverageReason.CONFLICT,
            {"contained_spans": 1, "overlapping_spans": 2},
        ),
        (
            [_segment(900, 1000), _segment(0, 1000)],
            CoverageReason.CONFLICT,
            {"contained_spans": 1, "overlapping_spans": 2},
        ),
        (
            [_segment(0, 600), _segment(500, 1000)],
            CoverageReason.CONFLICT,
            {"contained_spans": 0, "overlapping_spans": 2, "covered_samples": 1000},
        ),
        ([_segment(0, 1000, rate=44100)], CoverageReason.CONFLICT, {"rate_mismatch_rows": 1}),
        (
            [_segment(0, 1000), _segment(0, 1000, rate=44100)],
            CoverageReason.CONFLICT,
            {"rate_mismatch_rows": 1, "overlapping_spans": 0, "exact_duplicate_rows": 0},
        ),
        (
            [_segment(0, 499), _segment(499, 1000, recording_id="rec_b")],
            CoverageReason.GAP,
            {"uncovered_samples": 501, "foreign_rows": 1},
        ),
        ([_segment(0, 1000, recording_id="rec_b")], CoverageReason.EVIDENCE_ABSENT, None),
        (
            [_segment(MAX_COORDINATE, MAX_COORDINATE + 1)],
            CoverageReason.INSTANT_OUT_OF_DOMAIN,
            None,
        ),
        ([_segment(0, 1000), _segment(1000, 2000)], None, {"covered_samples": 1000}),
        (
            [_segment(0, 600), _segment(500, 700)],
            CoverageReason.CONFLICT,
            {"overlapping_spans": 2, "uncovered_samples": 300},
        ),
    ],
)
def test_r1_through_r13_stored_extent_and_capture_are_distinct(
    segments: list[dict[str, object]], reason: CoverageReason | None, detail: dict[str, int] | None
) -> None:
    """Bounded stored offset accounting cannot attest physical capture."""
    report = _coverage(_data(segments=segments))
    result = _extent(report)
    assert result.reason is reason
    if detail is None:
        assert result.detail is None
    else:
        dumped = _extent_counts(result).model_dump()
        assert {key: dumped[key] for key in detail} == detail
    assert [ref.seq for ref in result.evidence] == list(range(len(segments)))
    continuity = _outcome(report, CoverageCheckName.RECORDING_CAPTURE_CONTINUITY)
    assert continuity.reason is CoverageReason.PRODUCER_WITNESS_ABSENT
    assert continuity.status is CheckStatus.UNRESOLVED and continuity.evidence == result.evidence


@pytest.mark.parametrize("stream", list(CollectorStream))
@pytest.mark.parametrize("malformed", [False, True])
def test_i1_missing_and_malformed_streams_are_isolated(
    stream: CollectorStream, malformed: bool
) -> None:
    """Only scores and extent may change their own dependent results."""
    _, *fixture_rows = [json.loads(line) for line in _FIXTURE.read_bytes().splitlines()]
    inputs: _PAYLOADS = {
        row["stream"]: [cast(dict[str, object], row["payload"])] for row in fixture_rows
    }
    baseline = _coverage(_data(other=inputs))
    replacement: list[dict[str, object]] = [{}] if malformed else []
    if stream is CollectorStream.SHADOW_WINDOW:
        changed = _coverage(_data(windows=replacement, other=inputs))
        changed_indices = (0, 1)
    elif stream is CollectorStream.RECORDING_SEGMENT:
        changed = _coverage(_data(segments=replacement, other=inputs))
        changed_indices = (2, 3)
    else:
        changed = _coverage(_data(other=inputs | {stream.value: replacement}))
        changed_indices = ()
    for index, (before, after) in enumerate(zip(baseline.checks, changed.checks, strict=True)):
        if index in changed_indices:
            assert after.reason is (
                CoverageReason.EVIDENCE_MALFORMED if malformed else CoverageReason.EVIDENCE_ABSENT
            )
            assert after.evidence == () and after.detail is None
        else:
            assert after == before
        if index < 4:
            dependent = (
                CollectorStream.SHADOW_WINDOW if index < 2 else CollectorStream.RECORDING_SEGMENT
            )
            assert all(ref.stream is dependent for ref in after.evidence)


def test_i2_detector_and_recording_bindings_do_not_supply_one_another() -> None:
    """Changing one rate or identity cannot alter the other stream's report."""
    data = _data([_seconds_window()])
    detector = _clock_case()
    baseline = _coverage(data, _expect(detector=detector))
    assert _lattice(baseline).status is CheckStatus.SATISFIED
    for recording in (_recording(sample_rate_hz=8), _recording(recording_id="rec_b"), None):
        actual = _coverage(data, _expect(detector=detector, recording=recording))
        assert actual.checks[:2] == baseline.checks[:2]
        assert actual.checks[2:] != baseline.checks[2:]
    mutations = {
        "sample_rate_hz": 8,
        "origin_sample": 1,
        "charge_sample": 1,
        "drop_sample": 4,
        "hop_samples": 2,
        "length_samples": 3,
        "anchor": None,
    }
    for field, value in mutations.items():
        changed = _coverage(
            data,
            _expect(
                detector=DetectorLatticeBinding.model_validate(vars(detector) | {field: value})
            ),
        )
        assert changed.checks[2:] == baseline.checks[2:]
    assert _coverage(data, _expect(detector=None)).checks[2:] == baseline.checks[2:]


@pytest.mark.parametrize(
    "data, expectations, reason",
    [
        (_data([{}]), _expect(session_id="foreign"), CoverageReason.SCOPE_MISMATCH),
        (_data([{}]), _expect(detector=None), CoverageReason.EVIDENCE_MALFORMED),
        (_data([]), _expect(detector=None), CoverageReason.EXPECTATION_UNBOUND),
        (
            _data([_window(1), _seconds_window()]),
            _expect(),
            CoverageReason.CLOCK_ALIGNMENT_UNRESOLVED,
        ),
        (_data([_window(1), _skip(0)]), _expect(), CoverageReason.OFF_LATTICE),
        (
            _data([_window(0, decision_at=_at(7999)), _window(0, probability=0.5)]),
            _expect(),
            CoverageReason.CONFLICT,
        ),
    ],
)
def test_i3_lattice_fault_precedence(
    data: bytes, expectations: CoverageExpectations, reason: CoverageReason
) -> None:
    """An earlier fault controls the result even when later faults are present."""
    result = _lattice(_coverage(data, expectations))
    assert result.reason is reason and result.detail is None


@pytest.mark.parametrize(
    "rows, reason",
    [
        (
            [_segment(MAX_COORDINATE, MAX_COORDINATE + 1, recording_id="rec_b")],
            CoverageReason.EVIDENCE_ABSENT,
        ),
        (
            [_segment(MAX_COORDINATE, MAX_COORDINATE + 1, rate=44100)],
            CoverageReason.INSTANT_OUT_OF_DOMAIN,
        ),
    ],
)
def test_i3_extent_identity_and_domain_precede_conflict(
    rows: list[dict[str, object]], reason: CoverageReason
) -> None:
    """Extent precedence validates only selected ranges and checks their domain first."""
    result = _extent(_coverage(_data(segments=rows)))
    assert result.reason is reason and result.detail is None


def test_i4_two_full_streams_keep_bounded_original_order() -> None:
    """Maximal admitted window and segment streams preserve every original reference."""
    window = _skip(0, "pre_charge") | {"end_at": _at(1)}
    segment = _segment(0, 1, recording_id="r", rate=1)
    data = _data([window] * 8192, [segment] * 8192, session="s")
    assert len(data) <= 4 * 1024 * 1024
    report = _coverage(
        data,
        _expect(
            session_id="s",
            detector=_detector(charge_sample=1, drop_sample=3, hop_samples=1, length_samples=1),
            recording=_recording(recording_id="r", sample_rate_hz=1, required_end_sample=1),
        ),
    )
    expected_seqs = list(range(8192))
    for check in report.checks[:4]:
        assert [ref.seq for ref in check.evidence] == expected_seqs
    assert _lattice(report).reason is CoverageReason.SLOTS_MISSED
    assert _lattice_counts(_lattice(report)).duplicate_rows == 8191
    assert _extent(report).status is CheckStatus.SATISFIED
    assert _extent_counts(_extent(report)).exact_duplicate_rows == 8191
