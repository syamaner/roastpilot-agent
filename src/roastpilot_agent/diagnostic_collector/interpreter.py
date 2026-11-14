"""Interpret admitted synthetic evidence using caller-bound offline expectations."""

from fractions import Fraction
from typing import Iterator, TypeVar, cast  # noqa: UP035 - synthetic import allowlist

from pydantic import ValidationError

import roastpilot_agent.diagnostic_collector.bundle_reader as bundle_reader
from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AdmittedBundle,
    AlignmentAnchor,
    ClockName,
    CollectorAuthority,
    CollectorEnvelope,
    CollectorPayload,
    CollectorStream,
    CollectorStreamState,
    HumanFirstCrackPayload,
    IdentityPayload,
    Instant,
    RoomReadingPayload,
    ShadowEventPayload,
    ShadowWindowPayload,
    TemperatureTurnPayload,
)
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    MAX_COORDINATE,
    REASON_STATUS,
    CelsiusRange,
    CheckName,
    CheckReason,
    CheckResult,
    CheckStatus,
    ClockScale,
    ComponentIdentity,
    EvidenceRef,
    InterpretationInputError,
    InterpretationInputReason,
    InterpretationReport,
    SyntheticExpectations,
)

_CHECK_STREAMS: dict[CheckName, tuple[CollectorStream, ...]] = {
    CheckName.IDENTITY: (CollectorStream.IDENTITY,),
    CheckName.TEMPERATURE_PLAUSIBILITY: (CollectorStream.TEMPERATURE_TURN,),
    CheckName.TEMPERATURE_AGE: (CollectorStream.TEMPERATURE_TURN,),
    CheckName.ROOM_AGE: (CollectorStream.ROOM_READING,),
    CheckName.ROOM_PLACEMENT: (CollectorStream.ROOM_READING,),
    CheckName.HUMAN_FC: (CollectorStream.HUMAN_FC,),
    CheckName.SHADOW_TIMING: (CollectorStream.SHADOW_EVENT,),
    CheckName.SHADOW_CONFIRMATION: (CollectorStream.SHADOW_EVENT, CollectorStream.SHADOW_WINDOW),
    CheckName.OBSERVED_PROVENANCE: (),
}
_CLOCK_REASONS = (
    CheckReason.INSTANT_OUT_OF_DOMAIN,
    CheckReason.CLOCK_ALIGNMENT_UNRESOLVED,
    CheckReason.CLOCK_PATH_CONFLICT,
)
_Outcome = CheckReason | Fraction | None
_Payload = TypeVar("_Payload", bound=CollectorPayload)


class _ClockEdge:
    """An anchored affine mapping in the rate's declared source coordinates."""

    def __init__(self, anchor: AlignmentAnchor, scale: ClockScale | None, rate: int | None) -> None:
        source, target = anchor.source_clock, anchor.target_clock
        source_value, target_value = anchor.source_value, anchor.target_value
        if scale is not None and source is not scale.source_clock:
            source, target = target, source
            source_value, target_value = target_value, source_value
        self.source = source
        self.target = target
        self.source_value = Fraction(source_value)
        self.target_value = Fraction(target_value)
        if scale is not None:
            self.scale = Fraction(scale.target_per_source)
        else:
            sample_rate = cast(int, rate)  # Sample edges exist only with a bound rate.
            self.scale = (
                Fraction(1, sample_rate)
                if source is ClockName.RECORDING_SAMPLES
                else Fraction(sample_rate)
            )
        self.validity = None if scale is None else (scale.valid_from, scale.valid_to)

    def project(self, source: ClockName, point: Fraction) -> tuple[ClockName, Fraction] | None:
        if source is self.source:
            source_point = point
            destination = self.target
            projection = self.target_value + (point - self.source_value) * self.scale
        else:
            source_point = self.source_value + (point - self.target_value) / self.scale
            destination = self.source
            projection = source_point
        if self.validity is not None:
            low, high = self.validity
            if not (low <= self.source_value <= high and low <= source_point <= high):
                return None
        return destination, projection


class _Clocks:
    """Resolve a fixed target through all usable simple clock paths."""

    def __init__(
        self, anchors: tuple[AlignmentAnchor, ...], expectations: SyntheticExpectations
    ) -> None:
        self.rate = expectations.recording_sample_rate_hz
        self.edges: dict[ClockName, list[_ClockEdge]] = {clock: [] for clock in ClockName}
        scales = {
            frozenset((scale.source_clock, scale.target_clock)): scale
            for scale in expectations.clock_scales
        }
        for anchor in anchors:
            pair = frozenset((anchor.source_clock, anchor.target_clock))
            scale = scales.get(pair)
            if scale is None and (ClockName.RECORDING_SAMPLES not in pair or self.rate is None):
                continue
            edge = _ClockEdge(anchor, scale, self.rate)
            self.edges[edge.source].append(edge)
            self.edges[edge.target].append(edge)

    def point(self, instant: Instant, target: ClockName) -> Fraction | CheckReason:
        if instant.value > MAX_COORDINATE:
            return CheckReason.INSTANT_OUT_OF_DOMAIN
        if instant.clock is target:
            return Fraction(instant.value)

        values: set[Fraction] = set()
        paths = [(instant.clock, Fraction(instant.value), frozenset((instant.clock,)), False)]
        while paths:
            clock, point, visited, out_of_domain = paths.pop()
            for edge in self.edges[clock]:
                neighbor = edge.target if clock is edge.source else edge.source
                if neighbor in visited:
                    continue
                step = edge.project(clock, point)
                if step is None:
                    continue
                destination, projected = step
                outside = out_of_domain or not (
                    0 <= projected <= MAX_COORDINATE
                    and edge.source_value <= MAX_COORDINATE
                    and edge.target_value <= MAX_COORDINATE
                )
                if destination is target:
                    if outside:
                        return CheckReason.INSTANT_OUT_OF_DOMAIN
                    values.add(projected)
                else:
                    paths.append((destination, projected, visited | {destination}, outside))
        if not values:
            return CheckReason.CLOCK_ALIGNMENT_UNRESOLVED
        if len(values) > 1:
            return CheckReason.CLOCK_PATH_CONFLICT
        return values.pop()


class _Chronology:
    """Accumulate exact observations and precedence in the chosen metric clock."""

    def __init__(self, clocks: _Clocks, metric: Instant) -> None:
        self.clocks = clocks
        self.metric = metric
        self.last: Fraction | None = None
        self.reasons: set[CheckReason] = set()
        if metric.value > MAX_COORDINATE:
            self.reasons.add(CheckReason.INSTANT_OUT_OF_DOMAIN)
        if metric.clock is ClockName.RECORDING_SAMPLES and clocks.rate is None:
            self.reasons.add(CheckReason.CLOCK_ALIGNMENT_UNRESOLVED)

    def observe(self, instant: Instant) -> None:
        point = self.clocks.point(instant, self.metric.clock)
        if isinstance(point, CheckReason):
            self.reasons.add(point)
            return
        if self.last is not None and point < self.last:
            self.reasons.add(CheckReason.ORDER_CONFLICT)
        self.last = point

    def elapsed(self, *, age_maximum: int | float | None = None) -> Fraction | CheckReason:
        for reason in _CLOCK_REASONS:
            if reason in self.reasons:
                return reason
        if CheckReason.ORDER_CONFLICT in self.reasons:
            return CheckReason.ORDER_CONFLICT
        last = cast(Fraction, self.last)  # Called after a qualifying observation.
        reference = Fraction(self.metric.value)
        difference = last - reference if age_maximum is None else reference - last
        seconds = (
            difference / cast(int, self.clocks.rate)
            if self.metric.clock is ClockName.RECORDING_SAMPLES
            else difference
        )
        if seconds < 0:
            return CheckReason.ORDER_CONFLICT
        if age_maximum is not None and seconds > age_maximum:
            return CheckReason.STALE
        return seconds


def _reference(record: CollectorEnvelope) -> EvidenceRef:
    fields = type(record.payload).model_fields
    values = vars(record.payload)
    instants = tuple(values[name] for name in fields if isinstance(values[name], Instant))
    return EvidenceRef(stream=record.stream, seq=record.seq, instants=instants)


def _in_range(value: int | float | None, bounds: CelsiusRange) -> bool:
    number = cast(int | float, value)  # S1 requires a number on accepted/valid rows.
    return bounds.low_c <= number <= bounds.high_c


def _derived_seconds(outcome: _Outcome) -> float | None:
    return float(outcome) if isinstance(outcome, Fraction) else None


class _Evidence:
    """Read a check's dependent records while retaining bounded original references."""

    def __init__(self, bundle: AdmittedBundle, check: CheckName) -> None:
        self.entries = tuple(
            entry for entry in bundle.streams if entry.stream in _CHECK_STREAMS[check]
        )
        self.refs: list[EvidenceRef] = []

    def records(self, _payload_type: type[_Payload]) -> Iterator[_Payload]:
        (entry,) = self.entries
        for record in entry.records:
            self.refs.append(_reference(record))
            yield cast(_Payload, record.payload)  # S1 binds the payload type to this stream.

    def retain(self) -> None:
        self.refs.extend(_reference(record) for entry in self.entries for record in entry.records)


def _repeated(rows: Iterator[_Payload]) -> tuple[_Payload, bool]:
    first = next(rows)  # The dependent stream is admitted and nonempty.
    conflicting = False
    for row in rows:
        if row != first:
            conflicting = True
    return first, conflicting


class _Interpretation:
    """Evaluate each isolated check against a single admitted bundle."""

    def __init__(self, bundle: AdmittedBundle, expectations: SyntheticExpectations) -> None:
        self.bundle = bundle
        self.expected = expectations
        self.clocks = _Clocks(bundle.header.alignments, expectations)
        self.scope_matches = (
            bundle.header.session_id == expectations.session_id
            and bundle.header.bundle_id == expectations.bundle_id
        )

    def report(self) -> InterpretationReport:
        checks: dict[CheckName, CheckResult] = {}
        for check in CheckName:
            evidence = _Evidence(self.bundle, check)
            reason = self._prerequisite(check, evidence)
            if reason is None:
                outcome = self._evaluate(check, evidence, checks)
            else:
                evidence.retain()
                outcome = reason
            reason = outcome if isinstance(outcome, CheckReason) else None
            checks[check] = CheckResult(
                check=check,
                status=CheckStatus.SATISFIED if reason is None else REASON_STATUS[reason],
                reason=reason,
                evidence=tuple(evidence.refs),
                derived_seconds=_derived_seconds(outcome),
            )
        return InterpretationReport(
            bundle_sha256=self.bundle.bundle_sha256,
            authority=CollectorAuthority.NONE,
            checks=tuple(checks.values()),
        )

    def _prerequisite(self, check: CheckName, evidence: _Evidence) -> CheckReason | None:
        if check is CheckName.OBSERVED_PROVENANCE:
            return CheckReason.SYNTHETIC_EVIDENCE
        if not self.scope_matches:
            return CheckReason.SCOPE_MISMATCH
        if any(entry.state is CollectorStreamState.MALFORMED for entry in evidence.entries):
            return CheckReason.EVIDENCE_MALFORMED
        expected = self.expected
        required = {
            CheckName.IDENTITY: (expected.component,),
            CheckName.TEMPERATURE_PLAUSIBILITY: (expected.bt_range_c, expected.et_range_c),
            CheckName.TEMPERATURE_AGE: (
                expected.evaluation_at,
                expected.max_temperature_age_seconds,
            ),
            CheckName.ROOM_AGE: (
                expected.evaluation_at,
                expected.max_room_age_seconds,
                expected.room_range_c,
            ),
            CheckName.ROOM_PLACEMENT: (expected.placement_id,),
        }
        if any(value is None for value in required.get(check, ())):
            return CheckReason.EXPECTATION_UNBOUND
        if any(entry.state is CollectorStreamState.ABSENT for entry in evidence.entries):
            return CheckReason.EVIDENCE_ABSENT
        return None

    def _evaluate(
        self, check: CheckName, evidence: _Evidence, checks: dict[CheckName, CheckResult]
    ) -> _Outcome:
        if check is CheckName.IDENTITY:
            return self._identity(evidence)
        if check in (CheckName.TEMPERATURE_PLAUSIBILITY, CheckName.TEMPERATURE_AGE):
            return self._temperature(check, evidence)
        if check in (CheckName.ROOM_AGE, CheckName.ROOM_PLACEMENT):
            return self._room(check, evidence)
        if check is CheckName.HUMAN_FC:
            return self._human_fc(evidence)
        if check is CheckName.SHADOW_TIMING:
            return self._shadow_timing(evidence)
        return self._shadow_confirmation(evidence, checks[CheckName.SHADOW_TIMING])

    def _identity(self, evidence: _Evidence) -> CheckReason | None:
        first, conflicting = _repeated(evidence.records(IdentityPayload))
        if conflicting:
            return CheckReason.CONFLICT
        if vars(first) != vars(cast(ComponentIdentity, self.expected.component)):
            return CheckReason.EXPECTATION_MISMATCH
        return None

    def _temperature(self, check: CheckName, evidence: _Evidence) -> _Outcome:
        age = check is CheckName.TEMPERATURE_AGE
        chronology = (
            _Chronology(self.clocks, cast(Instant, self.expected.evaluation_at)) if age else None
        )
        selected: TemperatureTurnPayload | None = None
        turns: dict[str, TemperatureTurnPayload] = {}
        conflicting = False
        for row in evidence.records(TemperatureTurnPayload):
            if turns.setdefault(row.turn_id, row) != row:
                conflicting = True
            if row.status == "accepted":
                selected = row
                if chronology is not None:
                    chronology.observe(row.observed_at)
        if selected is None:
            return CheckReason.EVIDENCE_ABSENT
        if conflicting:
            return CheckReason.CONFLICT
        if chronology is not None:
            return chronology.elapsed(
                age_maximum=cast(int | float, self.expected.max_temperature_age_seconds)
            )
        if not (
            _in_range(selected.bt_c, cast(CelsiusRange, self.expected.bt_range_c))
            and _in_range(selected.et_c, cast(CelsiusRange, self.expected.et_range_c))
        ):
            return CheckReason.IMPLAUSIBLE
        return None

    def _room(self, check: CheckName, evidence: _Evidence) -> _Outcome:
        age = check is CheckName.ROOM_AGE
        chronology = (
            _Chronology(self.clocks, cast(Instant, self.expected.evaluation_at)) if age else None
        )
        selected: RoomReadingPayload | None = None
        for row in evidence.records(RoomReadingPayload):
            if row.status == "valid":
                selected = row
                if chronology is not None:
                    chronology.observe(row.observed_at)
        if selected is None:
            return CheckReason.EVIDENCE_ABSENT
        if chronology is None:
            if selected.placement_id is None:
                return CheckReason.PLACEMENT_ABSENT
            if selected.placement_id != self.expected.placement_id:
                return CheckReason.EXPECTATION_MISMATCH
            return None
        elapsed = chronology.elapsed(
            age_maximum=cast(int | float, self.expected.max_room_age_seconds)
        )
        if elapsed in (*_CLOCK_REASONS, CheckReason.ORDER_CONFLICT):
            return elapsed
        if not _in_range(selected.temperature_c, cast(CelsiusRange, self.expected.room_range_c)):
            return CheckReason.IMPLAUSIBLE
        return elapsed

    def _human_fc(self, evidence: _Evidence) -> CheckReason | None:
        first, conflicting = _repeated(evidence.records(HumanFirstCrackPayload))
        if conflicting:
            return CheckReason.CONFLICT
        if first.accepted_at.value > MAX_COORDINATE:
            return CheckReason.INSTANT_OUT_OF_DOMAIN
        return None

    def _shadow_timing(self, evidence: _Evidence) -> _Outcome:
        event, conflicting = _repeated(evidence.records(ShadowEventPayload))
        if conflicting:
            return CheckReason.CONFLICT
        chronology = _Chronology(self.clocks, event.onset_at)
        chronology.observe(event.decision_at)
        return chronology.elapsed()

    def _shadow_confirmation(self, evidence: _Evidence, timing: CheckResult) -> CheckReason | None:
        if timing.status is not CheckStatus.SATISFIED:
            evidence.retain()
            return CheckReason.DEPENDENCY_NOT_SATISFIED
        events, windows = evidence.entries
        event = cast(ShadowEventPayload, events.records[0].payload)
        evidence.refs.extend(_reference(record) for record in events.records)
        window: ShadowWindowPayload | None = None
        for record in windows.records:
            evidence.refs.append(_reference(record))
            if record.seq == event.confirming_window_seq:
                window = cast(ShadowWindowPayload, record.payload)
        if window is None:
            return CheckReason.EVIDENCE_ABSENT
        if window.eligibility != "eligible" or window.status != "confirmed":
            return CheckReason.CONFLICT
        decision = cast(Instant, window.decision_at)  # A confirmed S1 window has a decision.
        mapped = self.clocks.point(decision, event.decision_at.clock)
        if isinstance(mapped, CheckReason):
            return mapped
        if mapped != event.decision_at.value:
            return CheckReason.CONFLICT
        return None


def interpret_synthetic_bundle(
    data: bytes, *, expected_sha256: str, expectations: SyntheticExpectations
) -> InterpretationReport:
    """Interpret original synthetic evidence after one S1 admission.

    Args:
        data: Exact original bundle bytes.
        expected_sha256: Independently trusted digest, passed unchanged to S1.
        expectations: Independently bound session, thresholds, and clocks.

    Returns:
        The versioned report over admitted synthetic evidence.

    Raises:
        InterpretationInputError: If the original bytes or expectations are invalid.
        CollectorAdmissionError: If S1 refuses the original bundle.
    """
    if type(data) is not bytes:
        raise InterpretationInputError(InterpretationInputReason.DATA_NOT_BYTES)
    try:
        checked = SyntheticExpectations.model_validate(expectations)
    except ValidationError:
        raise InterpretationInputError(InterpretationInputReason.EXPECTATIONS_INVALID) from None
    bundle = bundle_reader.read_synthetic_bundle(data, expected_sha256=expected_sha256)
    return _Interpretation(bundle, checked).report()
