"""Interpret admitted synthetic model-score and stored recording coverage."""

from fractions import Fraction
from typing import cast

from pydantic import ValidationError

import roastpilot_agent.diagnostic_collector.bundle_reader as bundle_reader
from roastpilot_agent.diagnostic_collector._clock_projection import ClockProjection
from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AdmittedBundle,
    AlignmentAnchor,
    ClockName,
    CollectorEnvelope,
    CollectorStream,
    CollectorStreamEntry,
    CollectorStreamState,
    Instant,
    RecordingSegmentPayload,
    ShadowWindowPayload,
)
from roastpilot_agent.diagnostic_collector.coverage_schema import (
    COVERAGE_REASON_STATUS,
    CoverageCheckName,
    CoverageCheckResult,
    CoverageExpectations,
    CoverageReason,
    CoverageReport,
    DetectorLatticeBinding,
    LatticeCoverageDetail,
    RecordingBinding,
    RecordingExtentDetail,
)
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    MAX_COORDINATE,
    CheckReason,
    CheckStatus,
    EvidenceRef,
    InterpretationInputError,
    InterpretationInputReason,
)

_SAMPLES = ClockName.RECORDING_SAMPLES
_CLOCK_REASONS = {
    CheckReason.INSTANT_OUT_OF_DOMAIN: CoverageReason.INSTANT_OUT_OF_DOMAIN,
    CheckReason.CLOCK_ALIGNMENT_UNRESOLVED: CoverageReason.CLOCK_ALIGNMENT_UNRESOLVED,
    CheckReason.CLOCK_PATH_CONFLICT: CoverageReason.CLOCK_PATH_CONFLICT,
}
_WindowOutcome = tuple[CoverageReason | None, LatticeCoverageDetail | None]
_RecordingOutcome = tuple[CoverageReason | None, RecordingExtentDetail | None]


def _detector_clocks(
    bundle: AdmittedBundle, expectations: CoverageExpectations, detector: DetectorLatticeBinding
) -> ClockProjection:
    anchors = tuple(
        anchor
        for anchor in bundle.header.alignments
        if _SAMPLES not in (anchor.source_clock, anchor.target_clock)
    )
    if detector.anchor is not None:
        source = detector.anchor
        anchors += (
            AlignmentAnchor(
                source_clock=source.seconds_clock,
                source_value=source.seconds_value,
                target_clock=_SAMPLES,
                target_value=source.sample_value,
            ),
        )
    return ClockProjection(anchors, expectations.clock_scales, detector.sample_rate_hz)


def _lattice_coverage(
    records: tuple[CollectorEnvelope, ...],
    detector: DetectorLatticeBinding,
    clocks: ClockProjection,
) -> _WindowOutcome:
    origin, charge, drop = detector.origin_sample, detector.charge_sample, detector.drop_sample
    hop, length = detector.hop_samples, detector.length_samples
    first = max(0, -((origin - charge) // hop))
    last = (drop - 1 - length - origin) // hop
    expected = max(0, last - first + 1)
    clock_failures: set[CheckReason] = set()
    slots: dict[int, ShadowWindowPayload] = {}
    off_lattice = conflict = bad_order = False
    duplicates = ineligible = 0

    for record in records:
        row = cast(ShadowWindowPayload, record.payload)  # S1 binds payload type to stream.
        start = clocks.point(row.start_at, _SAMPLES)
        end = clocks.point(row.end_at, _SAMPLES)
        decision = None if row.decision_at is None else clocks.point(row.decision_at, _SAMPLES)
        failures = {point for point in (start, end, decision) if isinstance(point, CheckReason)}
        if failures:
            clock_failures.update(failures)
            continue
        start, end = cast(Fraction, start), cast(Fraction, end)
        if (
            start.denominator != 1
            or end.denominator != 1
            or start < origin
            or (int(start) - origin) % hop != 0
            or end - start != length
        ):
            off_lattice = True
            continue

        label = "post_drop" if end >= drop else "pre_charge" if start < charge else "eligible"
        if row.eligibility != label:
            conflict = True
        if label == "eligible" and isinstance(decision, Fraction) and decision < end:
            bad_order = True
        slot = (int(start) - origin) // hop
        previous = slots.get(slot)
        if previous is None:
            slots[slot] = row
        elif previous == row:
            duplicates += 1
        else:
            conflict = True
        if label != "eligible":
            ineligible += 1

    for source, target in _CLOCK_REASONS.items():
        if source in clock_failures:
            return target, None
    if off_lattice:
        return CoverageReason.OFF_LATTICE, None
    if conflict:
        return CoverageReason.CONFLICT, None
    if bad_order:
        return CoverageReason.ORDER_CONFLICT, None

    scored = sum(
        first <= slot <= last and row.status in ("negative", "candidate", "confirmed")
        for slot, row in slots.items()
    )
    errored = sum(first <= slot <= last and row.status == "error" for slot, row in slots.items())
    missed = expected - scored
    detail = LatticeCoverageDetail(
        expected_slots=expected,
        scored_slots=scored,
        errored_slots=errored,
        missed_slots=missed,
        duplicate_rows=duplicates,
        ineligible_rows=ineligible,
    )
    if expected == 0:
        return CoverageReason.EMPTY_DENOMINATOR, detail
    return (CoverageReason.SLOTS_MISSED if missed else None), detail


def _recording_coverage(
    records: tuple[CollectorEnvelope, ...], binding: RecordingBinding
) -> _RecordingOutcome:
    rows = tuple(cast(RecordingSegmentPayload, record.payload) for record in records)
    selected = tuple(row for row in rows if row.recording_id == binding.recording_id)
    if not selected:
        return CoverageReason.EVIDENCE_ABSENT, None
    triples = {(row.first_sample, row.sample_count, row.sample_rate_hz) for row in selected}
    # Descending ends for equal starts let the prefix detect strict containment.
    spans = sorted({(start, -(start + count)) for start, count, _ in triples})
    if any(-end > MAX_COORDINATE for _, end in spans):
        return CoverageReason.INSTANT_OUT_OF_DOMAIN, None

    low, high = binding.required_first_sample, binding.required_end_sample
    covered = contained = overlapping = prefix_end = 0
    following = iter(spans[1:])
    next_start, _ = next(following, (MAX_COORDINATE, 0))
    for start, negative_end in spans:
        end = -negative_end
        contained += end <= prefix_end
        overlapping += prefix_end > start or end > next_start
        covered += max(0, min(end, high) - max(start, prefix_end, low))
        prefix_end = max(prefix_end, end)
        next_start, _ = next(following, (MAX_COORDINATE, 0))

    required = high - low
    uncovered = required - covered
    rate_mismatches = sum(row.sample_rate_hz != binding.sample_rate_hz for row in selected)
    detail = RecordingExtentDetail(
        required_samples=required,
        covered_samples=covered,
        uncovered_samples=uncovered,
        selected_rows=len(selected),
        foreign_rows=len(rows) - len(selected),
        exact_duplicate_rows=len(selected) - len(triples),
        rate_mismatch_rows=rate_mismatches,
        contained_spans=contained,
        overlapping_spans=overlapping,
    )
    if rate_mismatches or overlapping:
        return CoverageReason.CONFLICT, detail
    return (CoverageReason.GAP if uncovered else None), detail


def _reference(record: CollectorEnvelope) -> EvidenceRef:
    fields = type(record.payload).model_fields
    values = vars(record.payload)
    instants = tuple(values[name] for name in fields if isinstance(values[name], Instant))
    return EvidenceRef(stream=record.stream, seq=record.seq, instants=instants)


def _prerequisite(
    scope_matches: bool, entry: CollectorStreamEntry, binding: object
) -> CoverageReason | None:
    if not scope_matches:
        return CoverageReason.SCOPE_MISMATCH
    if entry.state is CollectorStreamState.MALFORMED:
        return CoverageReason.EVIDENCE_MALFORMED
    if binding is None:
        return CoverageReason.EXPECTATION_UNBOUND
    if entry.state is CollectorStreamState.ABSENT:
        return CoverageReason.EVIDENCE_ABSENT
    return None


def _result(
    check: CoverageCheckName,
    reason: CoverageReason | None,
    evidence: tuple[EvidenceRef, ...] = (),
    detail: LatticeCoverageDetail | RecordingExtentDetail | None = None,
) -> CoverageCheckResult:
    return CoverageCheckResult(
        check=check,
        status=CheckStatus.SATISFIED if reason is None else COVERAGE_REASON_STATUS[reason],
        reason=reason,
        evidence=evidence,
        detail=detail,
    )


def _interpret(bundle: AdmittedBundle, expectations: CoverageExpectations) -> CoverageReport:
    scope = (
        bundle.header.session_id == expectations.session_id
        and bundle.header.bundle_id == expectations.bundle_id
    )
    streams = {entry.stream: entry for entry in bundle.streams}
    windows = streams[CollectorStream.SHADOW_WINDOW]
    segments = streams[CollectorStream.RECORDING_SEGMENT]
    detector, recording = expectations.detector, expectations.recording
    detector_reason = _prerequisite(scope, windows, detector)
    recording_reason = _prerequisite(scope, segments, recording)
    window_reason, lattice = (
        _lattice_coverage(
            windows.records, detector, _detector_clocks(bundle, expectations, detector)
        )
        if detector_reason is None and detector is not None
        else (detector_reason, None)
    )
    extent_reason, extent = (
        _recording_coverage(segments.records, recording)
        if recording_reason is None and recording is not None
        else (recording_reason, None)
    )
    window_refs = tuple(_reference(record) for record in windows.records)
    segment_refs = tuple(_reference(record) for record in segments.records)
    return CoverageReport(
        bundle_sha256=bundle.bundle_sha256,
        checks=(
            _result(CoverageCheckName.LATTICE_SCORE_COVERAGE, window_reason, window_refs, lattice),
            _result(
                CoverageCheckName.DETECTOR_CAPTURE_CONTINUITY,
                detector_reason or CoverageReason.PRODUCER_WITNESS_ABSENT,
                window_refs,
            ),
            _result(
                CoverageCheckName.RECORDING_EXTENT_COVERAGE, extent_reason, segment_refs, extent
            ),
            _result(
                CoverageCheckName.RECORDING_CAPTURE_CONTINUITY,
                recording_reason or CoverageReason.PRODUCER_WITNESS_ABSENT,
                segment_refs,
            ),
            _result(CoverageCheckName.OBSERVED_PROVENANCE, CoverageReason.SYNTHETIC_EVIDENCE),
        ),
    )


def interpret_coverage_bundle(
    data: bytes, *, expected_sha256: str, expectations: CoverageExpectations
) -> CoverageReport:
    """Evaluate in-memory synthetic evidence after one S1 admission.

    Args:
        data: Unmodified canonical S1 bundle bytes.
        expected_sha256: Independently supplied digest passed through to S1.
        expectations: Independently bound synthetic coverage expectations.

    Returns:
        A closed report about the synthetic lattice and stored offsets.

    Raises:
        InterpretationInputError: If the bytes type or expectations are invalid.
        CollectorAdmissionError: If S1 refuses the bundle.
    """
    if type(data) is not bytes:
        raise InterpretationInputError(InterpretationInputReason.DATA_NOT_BYTES)
    try:
        expected = CoverageExpectations.model_validate(expectations)
    except ValidationError:
        raise InterpretationInputError(InterpretationInputReason.EXPECTATIONS_INVALID) from None
    bundle = bundle_reader.read_synthetic_bundle(data, expected_sha256=expected_sha256)
    return _interpret(bundle, expected)
