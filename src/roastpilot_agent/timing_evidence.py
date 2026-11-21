"""Pure, closed grammar for synthetic P1 timing evidence.

This readback grants no operational or physical qualification. The independent
text bound does not bound SDK buffers or recover duplicate structured keys.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import cast


class TimingEvidenceReadOutcome(Enum):
    """Closed read and admission outcomes."""

    NOT_ATTEMPTED = "not_attempted"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    REFUSED_IDENTITY = "refused_identity"
    REFUSED_CAPABILITY = "refused_capability"
    REFUSED_BUSY = "refused_busy"
    PRODUCER_UNAVAILABLE = "unavailable"
    IDENTITY_REJECTED = "identity_rejected"
    SEQUENCE_REJECTED = "sequence_rejected"
    EVENT_IDENTITY_REJECTED = "event_identity_rejected"
    EVIDENCE = "evidence"


class ProducerReason(Enum):
    """Value-free reasons mirrored from protected P1."""

    UNKNOWN_SESSION = "unknown_session"
    NOT_LATEST_SESSION = "not_latest_session"
    SESSION_FAULTED = "session_faulted"
    SESSION_NOT_ACTIVE = "session_not_active"
    SESSION_PURPOSE_NOT_ELIGIBLE = "session_purpose_not_eligible"
    SESSION_CHANGED_DURING_READ = "session_changed_during_read"
    CAPABILITY_UNREADABLE = "capability_unreadable"
    CAPABILITY_MALFORMED = "capability_malformed"
    CAPABILITY_ABSENT = "capability_absent"
    CAPABILITY_VERSION_UNSUPPORTED = "capability_version_unsupported"
    COMPOSITION_NOT_QUALIFIED = "composition_not_qualified"
    FINALISATION_IN_PROGRESS = "finalisation_in_progress"
    DROP_IN_PROGRESS = "drop_in_progress"
    CLOCK_INVALID = "clock_invalid"
    DRIVER_RAISED = "driver_raised"
    DRIVER_RESULT_INVALID = "driver_result_invalid"
    DRIVER_NOT_CONNECTED = "driver_not_connected"
    CLOCK_REGRESSION = "clock_regression"
    OUT_OF_ORDER_READ = "out_of_order_read"
    SEQUENCE_EXHAUSTED = "sequence_exhausted"
    RECORD_INVALID = "record_invalid"


class EventSource(Enum):
    """Reported provenance, without operational promotion."""

    MANUAL_MARK = "manual_mark"
    AUTO_T0 = "auto_t0"
    MODEL_DETECTION = "model_detection"
    UNATTRIBUTED = "unattributed"


class TimeBasis(Enum):
    """Closed event time bases."""

    PRODUCER_CANONICAL_RECORD = "producer_canonical_record"
    DETECTOR_CLOCK_REBASED = "detector_clock_rebased"


class Qualification(Enum):
    """The sole supported, synthetic qualification."""

    SYNTHETIC_MOCK_ONLY = "none_synthetic_mock_only"


@dataclass(frozen=True, slots=True)
class EventEvidence:
    """Validated canonical event identity."""

    kind: str
    event_index: int
    canonical_seconds: float
    clock_epoch_id: str
    source: EventSource
    time_basis: TimeBasis


@dataclass(frozen=True, slots=True)
class ValidChannel:
    """Synthetic in-call channel bracket."""

    celsius: float
    acquisition_lo_seconds: float
    acquisition_hi_seconds: float


@dataclass(frozen=True, slots=True)
class UnavailableChannel:
    """Driver reported no value."""

    reason: str = "reported_none"


@dataclass(frozen=True, slots=True)
class InvalidChannel:
    """Driver reported an invalid value, retained as a closed reason."""

    reason: str


ChannelEvidence = ValidChannel | UnavailableChannel | InvalidChannel


@dataclass(frozen=True, slots=True)
class TimingEvidence:
    """Validated synthetic record bound to a local child epoch."""

    child_epoch: int
    session_id: str
    session_ownership_sequence: int
    epoch_id: str
    evidence_sequence: int
    phase: str
    charge: EventEvidence | None
    first_crack: EventEvidence | None
    drop: EventEvidence | None
    bean: ChannelEvidence
    environment: ChannelEvidence
    qualification: Qualification = Qualification.SYNTHETIC_MOCK_ONLY


@dataclass(frozen=True, slots=True)
class TimingEvidenceRead:
    """A read result carrying only validated data and closed outcomes."""

    outcome: TimingEvidenceReadOutcome
    producer_reason: ProducerReason | None = None
    record: TimingEvidence | None = None


@dataclass(frozen=True, slots=True)
class TimingEvidencePin:
    """Latched identity and last admitted chronology."""

    record: TimingEvidence
    bracket: tuple[float, float] | None


def _object(value: object, keys: str) -> dict[str, object]:
    if type(value) is not dict or value.keys() != set(keys.split()):
        raise ValueError
    return cast("dict[str, object]", value)


def _string(value: object) -> str:
    if type(value) is not str:
        raise ValueError
    return value


def _integer(value: object, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError
    return value


def _float(value: object, *, nonnegative: bool = True) -> float:
    if type(value) is not float or not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError
    return value


def _literal(value: object, allowed: str) -> str:
    result = _string(value)
    if result not in allowed.split():
        raise ValueError
    return result


def _epoch(value: object) -> str:
    result = _string(value)
    if re.fullmatch(r"[0-9a-f]{32}", result, flags=re.ASCII) is None:
        raise ValueError
    return result


_RECORD_KEYS = (
    "session_id session_ownership_sequence driver clock "
    "evidence_sequence phase charge first_crack drop bean environment "
    "time_basis physical_qualification"
)
_EVENT_KEYS = (
    "kind event_index canonical_seconds clock_epoch_id source time_basis physical_time_bound"
)
_VALID_CHANNEL_KEYS = (
    "status celsius acquisition_lo_seconds acquisition_hi_seconds "
    "clock_epoch_id basis qualification"
)


def _channel(value: object, epoch: str) -> ChannelEvidence:
    # Both public entry points validate the closed shape before this parser.
    raw = cast("dict[str, object]", value)
    status = _literal(raw.get("status"), "valid unavailable invalid")
    if status != "valid":
        raw = _object(raw, "status reason")
        if status == "unavailable":
            _literal(raw["reason"], "reported_none")
            return UnavailableChannel()
        return InvalidChannel(_literal(raw["reason"], "not_exact_float non_finite"))
    raw = _object(
        raw,
        (_VALID_CHANNEL_KEYS),
    )
    if _epoch(raw["clock_epoch_id"]) != epoch:
        raise ValueError
    _literal(raw["basis"], "in_call_synthesis")
    _literal(raw["qualification"], "synthetic_software_only")
    lo, hi = _float(raw["acquisition_lo_seconds"]), _float(raw["acquisition_hi_seconds"])
    if lo > hi:
        raise ValueError
    return ValidChannel(_float(raw["celsius"], nonnegative=False), lo, hi)


def _event(value: object, kind: str, epoch: str) -> EventEvidence | None:
    if value is None:
        return None
    raw = _object(
        value,
        _EVENT_KEYS,
    )
    _literal(raw["kind"], kind)
    if _epoch(raw["clock_epoch_id"]) != epoch:
        raise ValueError
    _literal(raw["physical_time_bound"], "not_provided")
    allowed = {
        "beans_added": "manual_mark auto_t0 unattributed",
        "first_crack_detected": "manual_mark model_detection unattributed",
        "beans_dropped": "unattributed",
    }
    source = EventSource(_literal(raw["source"], allowed[kind]))
    basis = TimeBasis(_string(raw["time_basis"]))
    expected = (
        TimeBasis.DETECTOR_CLOCK_REBASED
        if source is EventSource.MODEL_DETECTION
        else TimeBasis.PRODUCER_CANONICAL_RECORD
    )
    if basis is not expected:
        raise ValueError
    return EventEvidence(
        kind,
        _integer(raw["event_index"], 0),
        _float(raw["canonical_seconds"]),
        epoch,
        source,
        basis,
    )


def _record_shape(value: object) -> None:
    """Bound recursive comparison to closed objects and exact scalar leaves."""
    raw = _object(value, _RECORD_KEYS)
    for key in ("session_id", "driver", "phase", "time_basis", "physical_qualification"):
        _string(raw[key])
    for key in ("session_ownership_sequence", "evidence_sequence"):
        _integer(raw[key])
    clock = _object(raw["clock"], "epoch_id domain suspend_behaviour")
    for item in clock.values():
        _string(item)
    for slot in ("charge", "first_crack", "drop"):
        if raw[slot] is None:
            continue
        event = _object(raw[slot], _EVENT_KEYS)
        _integer(event["event_index"], 0)
        _float(event["canonical_seconds"])
        for key in ("kind", "clock_epoch_id", "source", "time_basis", "physical_time_bound"):
            _string(event[key])
    for slot in ("bean", "environment"):
        channel = raw[slot]
        if type(channel) is not dict:
            raise ValueError
        channel = cast("dict[str, object]", channel)
        status = _literal(channel.get("status"), "valid unavailable invalid")
        if status == "valid":
            channel = _object(channel, _VALID_CHANNEL_KEYS)
            for key in ("celsius", "acquisition_lo_seconds", "acquisition_hi_seconds"):
                _float(channel[key], nonnegative=False)
            for key in ("clock_epoch_id", "basis", "qualification"):
                _string(channel[key])
        else:
            channel = _object(channel, "status reason")
            _string(channel["reason"])


def _record(value: object, child_epoch: int) -> TimingEvidence:
    raw = _object(
        value,
        (_RECORD_KEYS),
    )
    session_id = _string(raw["session_id"])
    if not 1 <= len(session_id) <= 128:
        raise ValueError
    _literal(raw["driver"], "mock")
    _literal(raw["time_basis"], "canonical_operational_session_relative")
    _literal(raw["physical_qualification"], "none_synthetic_mock_only")
    clock = _object(raw["clock"], "epoch_id domain suspend_behaviour")
    epoch = _epoch(clock["epoch_id"])
    _literal(clock["domain"], "session_relative_store_monotonic")
    _literal(clock["suspend_behaviour"], "unqualified")
    phase = _literal(raw["phase"], "pre_roast roasting development dropped cooling")
    charge, fc, drop = (
        _event(raw[slot], kind, epoch)
        for slot, kind in (
            ("charge", "beans_added"),
            ("first_crack", "first_crack_detected"),
            ("drop", "beans_dropped"),
        )
    )
    flags = (charge is not None, fc is not None, drop is not None)
    table = {
        "pre_roast": (False, False, False),
        "roasting": (True, False, False),
        "development": (True, True, False),
    }
    if phase in table and flags != table[phase]:
        raise ValueError
    if phase in ("dropped", "cooling") and (charge is None or drop is None):
        raise ValueError
    events = [e for e in (charge, fc, drop) if e is not None]
    if any(
        b.event_index <= a.event_index or b.canonical_seconds < a.canonical_seconds
        for a, b in zip(events, events[1:], strict=False)
    ):
        raise ValueError
    bean, environment = _channel(raw["bean"], epoch), _channel(raw["environment"], epoch)
    if (
        isinstance(bean, ValidChannel)
        and isinstance(environment, ValidChannel)
        and _bracket_channel(bean) != _bracket_channel(environment)
    ):
        raise ValueError
    return TimingEvidence(
        child_epoch,
        session_id,
        _integer(raw["session_ownership_sequence"]),
        epoch,
        _integer(raw["evidence_sequence"]),
        phase,
        charge,
        fc,
        drop,
        bean,
        environment,
    )


def _bracket_channel(channel: ValidChannel) -> tuple[float, float]:
    return channel.acquisition_lo_seconds, channel.acquisition_hi_seconds


def _bracket(record: TimingEvidence) -> tuple[float, float] | None:
    for channel in (record.bean, record.environment):
        if isinstance(channel, ValidChannel):
            return _bracket_channel(channel)
    return None


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_: str) -> object:
    raise ValueError


def _finite(text: str) -> float:
    return _float(float(text), nonnegative=False)


def _same(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        a, b = cast("dict[str, object]", left), cast("dict[str, object]", right)
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if type(left) is float:
        return math.isfinite(left) and left.hex() == cast("float", right).hex()
    return type(left) in (str, int, bool, type(None)) and left == right


_ROWS = {
    "refused_identity": (
        "unknown_session not_latest_session session_faulted "
        "session_not_active session_purpose_not_eligible session_changed_during_read"
    ),
    "refused_capability": (
        "capability_unreadable capability_malformed "
        "capability_absent capability_version_unsupported composition_not_qualified"
    ),
    "refused_busy": "finalisation_in_progress drop_in_progress",
    "unavailable": (
        "clock_invalid driver_raised driver_result_invalid driver_not_connected "
        "clock_regression out_of_order_read sequence_exhausted record_invalid"
    ),
}


def decode_timing_evidence_reply(
    is_error: object, content: object, structured: object, session_id: str, child_epoch: int
) -> TimingEvidenceRead:
    """Decode both visible members independently, without output-schema trust."""
    outcome = TimingEvidenceReadOutcome
    malformed = TimingEvidenceRead(outcome.MALFORMED_RESPONSE)
    try:
        if is_error is not False or type(content) is not list:
            return malformed
        content = cast("list[object]", content)
        if len(content) != 1:
            return malformed
        block = _object(content[0], "type text")
        _literal(block["type"], "text")
        text = _string(block["text"])
        if len(text) > 16_384:
            return malformed
        decoded: object = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_finite
        )
        keys = (
            "evidence_protocol evidence_version outcome reason_code "
            "driver_read requested_session_id record"
        )
        raw, mirror = _object(structured, keys), _object(decoded, keys)
        # Check the complete nested closed grammar before recursive comparison.
        # Protocol scalars remain unclassified here so bool versions can receive
        # the specified UNSUPPORTED_PROTOCOL outcome after mirror agreement.
        for member in (raw, mirror):
            for key in ("evidence_protocol", "evidence_version"):
                if type(member[key]) not in (str, int, float, bool, type(None)):
                    return malformed
            _string(member["requested_session_id"])
            _string(member["outcome"])
            _string(member["driver_read"])
            if member["reason_code"] is not None:
                _string(member["reason_code"])
            if member["record"] is not None:
                _record_shape(member["record"])
        if not _same(raw, mirror):
            return malformed
        if (
            type(raw["evidence_protocol"]) is not str
            or raw["evidence_protocol"] != "timing_evidence/v1"
            or type(raw["evidence_version"]) is not int
            or raw["evidence_version"] != 1
        ):
            return TimingEvidenceRead(outcome.UNSUPPORTED_PROTOCOL)
        if _string(raw["requested_session_id"]) != session_id:
            return TimingEvidenceRead(outcome.IDENTITY_REJECTED)
        row = _literal(
            raw["outcome"], "evidence refused_identity refused_capability refused_busy unavailable"
        )
        read = _literal(raw["driver_read"], "invoked not_invoked")
        if row == "evidence":
            if raw["reason_code"] is not None or read != "invoked":
                return malformed
            record = _record(raw["record"], child_epoch)
            if record.session_id != session_id:
                return TimingEvidenceRead(outcome.IDENTITY_REJECTED)
            return TimingEvidenceRead(outcome.EVIDENCE, record=record)
        reason = ProducerReason(_literal(raw["reason_code"], _ROWS[row]))
        if raw["record"] is not None or (row == "refused_capability" and read != "not_invoked"):
            return malformed
        if row == "refused_identity" and (read == "invoked") != (
            reason is ProducerReason.SESSION_CHANGED_DURING_READ
        ):
            return malformed
        if (
            row == "unavailable"
            and reason is not ProducerReason.CLOCK_INVALID
            and read != "invoked"
        ):
            return malformed
        return TimingEvidenceRead(outcome(row), reason)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return malformed


def _event_identity(event: EventEvidence | None) -> object:
    return (
        None
        if event is None
        else (event.event_index, event.canonical_seconds.hex(), event.source, event.time_basis)
    )


def _check_typed_record(record: object) -> TimingEvidence:
    if type(record) is not TimingEvidence:
        raise ValueError
    _integer(record.child_epoch)
    if record.qualification is not Qualification.SYNTHETIC_MOCK_ONLY:
        raise ValueError
    events: dict[str, object] = {}
    for slot, event in (
        ("charge", record.charge),
        ("first_crack", record.first_crack),
        ("drop", record.drop),
    ):
        if event is None:
            events[slot] = None
            continue
        if (
            type(event) is not EventEvidence
            or type(event.source) is not EventSource
            or type(event.time_basis) is not TimeBasis
        ):
            raise ValueError
        events[slot] = dict(
            kind=event.kind,
            event_index=event.event_index,
            canonical_seconds=event.canonical_seconds,
            clock_epoch_id=event.clock_epoch_id,
            source=event.source.value,
            time_basis=event.time_basis.value,
            physical_time_bound="not_provided",
        )
    channels: dict[str, object] = {}
    for slot, channel in (("bean", record.bean), ("environment", record.environment)):
        if type(channel) is ValidChannel:
            channels[slot] = dict(
                status="valid",
                celsius=channel.celsius,
                acquisition_lo_seconds=channel.acquisition_lo_seconds,
                acquisition_hi_seconds=channel.acquisition_hi_seconds,
                clock_epoch_id=record.epoch_id,
                basis="in_call_synthesis",
                qualification="synthetic_software_only",
            )
        elif type(channel) is UnavailableChannel:
            channels[slot] = dict(status="unavailable", reason=channel.reason)
        elif type(channel) is InvalidChannel:
            channels[slot] = dict(status="invalid", reason=channel.reason)
        else:
            raise ValueError
    return _record(
        dict(
            session_id=record.session_id,
            session_ownership_sequence=record.session_ownership_sequence,
            driver="mock",
            clock=dict(
                epoch_id=record.epoch_id,
                domain="session_relative_store_monotonic",
                suspend_behaviour="unqualified",
            ),
            evidence_sequence=record.evidence_sequence,
            phase=record.phase,
            **events,
            **channels,
            time_basis="canonical_operational_session_relative",
            physical_qualification="none_synthetic_mock_only",
        ),
        record.child_epoch,
    )


def admit_record(
    record: object, pin: TimingEvidencePin | None
) -> tuple[TimingEvidenceReadOutcome, TimingEvidencePin | None]:
    """Latch identity and advance sequence atomically in the calling task."""
    outcome = TimingEvidenceReadOutcome
    try:
        record = _check_typed_record(record)
        if pin is not None:
            if type(pin) is not TimingEvidencePin:
                raise ValueError
            _check_typed_record(pin.record)
            if pin.bracket is not None:
                if type(pin.bracket) is not tuple or len(pin.bracket) != 2:
                    raise ValueError
                lo, hi = (_float(value) for value in pin.bracket)
                if lo > hi:
                    raise ValueError
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        return outcome.MALFORMED_RESPONSE, pin
    bracket = _bracket(record)
    if pin is not None:
        old = pin.record
        if (
            record.child_epoch,
            record.session_id,
            record.session_ownership_sequence,
            record.epoch_id,
        ) != (old.child_epoch, old.session_id, old.session_ownership_sequence, old.epoch_id):
            return outcome.IDENTITY_REJECTED, pin
        if record.evidence_sequence <= old.evidence_sequence or (
            bracket is not None
            and pin.bracket is not None
            and any(a < b for a, b in zip(bracket, pin.bracket, strict=True))
        ):
            return outcome.SEQUENCE_REJECTED, pin
        for previous, current in zip(
            (old.charge, old.first_crack, old.drop),
            (record.charge, record.first_crack, record.drop),
            strict=True,
        ):
            if previous is not None and _event_identity(previous) != _event_identity(current):
                return outcome.EVENT_IDENTITY_REJECTED, pin
    return outcome.EVIDENCE, TimingEvidencePin(
        record, bracket if bracket is not None else None if pin is None else pin.bracket
    )
