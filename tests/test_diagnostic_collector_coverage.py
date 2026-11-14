"""Synthetic compatibility and coverage contracts for D230 S2b."""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import roastpilot_agent.diagnostic_collector as collector
from roastpilot_agent.diagnostic_collector import (
    SyntheticExpectations,
    interpret_synthetic_bundle,
)
from roastpilot_agent.diagnostic_collector.bundle_schema import ClockName, Instant
from roastpilot_agent.diagnostic_collector.interpretation_schema import MAX_COORDINATE, ClockScale

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
