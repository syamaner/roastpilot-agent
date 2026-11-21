"""Closed, ephemeral authority for the private synthetic pre-FC composition.

No receipt proves physical effect. Nothing here persists or restores authority.
"""

import math
from dataclasses import dataclass
from enum import Enum, auto
from typing import cast

from roastpilot_agent.human_bootstrap import _single_text  # pyright: ignore[reportPrivateUsage]
from roastpilot_agent.timing_evidence import (
    EventSource,
    Qualification,
    TimeBasis,
    TimingEvidence,
)


class PermitState(Enum):
    """One-way run-local permission states."""

    ACTIVE = auto()
    MARK_RESERVED = auto()
    ENDED = auto()


class PairOutcome(Enum):
    """Truthful outcomes of independently acknowledged lever writes."""

    UNSENT = auto()
    HEAT_CONFIRMED = auto()
    BOTH_CONFIRMED = auto()
    DISPLACED = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class LeverReceipt:
    """Strictly acknowledged command values; no assertion of physical effect."""

    session_id: str
    phase: str
    heat_percent: int
    fan_percent: int
    cooling_on: bool


@dataclass(frozen=True, slots=True)
class PrivateBinding:
    """Exact objects and generations captured at durable Start settlement."""

    run: object
    adapter: object
    child: object
    sdk_session: object
    session_id: str
    child_epoch: int
    identity_generation: int
    command_generation: int


@dataclass(frozen=True, slots=True)
class MarkClaim:
    """Identity-based reservation for one operator mark attempt."""

    binding: PrivateBinding


class PreFcPermission:
    """Own a single grant and irreversible mark reservation or revocation."""

    def __init__(self, binding: PrivateBinding) -> None:
        self.binding = binding
        self.state = PermitState.ACTIVE
        self.claim: MarkClaim | None = None
        self.pair_outcome = PairOutcome.UNSENT
        self.heat_receipt: LeverReceipt | None = None
        self.fan_receipt: LeverReceipt | None = None

    def end(self) -> None:
        """Irreversibly withhold routine authority."""
        self.state = PermitState.ENDED

    def reserve_mark(self) -> MarkClaim | None:
        """Consume the only mark slot and terminate routine lever eligibility."""
        if self.state is not PermitState.ACTIVE:
            return None
        self.state = PermitState.MARK_RESERVED
        self.claim = MarkClaim(self.binding)
        return self.claim


def _same(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        a, b = cast("dict[str, object]", left), cast("dict[str, object]", right)
        return a.keys() == b.keys() and all(_same(v, b[k]) for k, v in a.items())
    if type(left) is list:
        a_list, b_list = cast("list[object]", left), cast("list[object]", right)
        return len(a_list) == len(b_list) and all(
            _same(a, b) for a, b in zip(a_list, b_list, strict=True)
        )
    if type(left) is float:
        return math.isfinite(left) and left.hex() == cast("float", right).hex()
    return left == right


def decode_private_receipt(
    name: str,
    is_error: object,
    content: object,
    structured: object,
    session_id: str,
    value: int | None = None,
) -> dict[str, object] | None:
    """Validate three fixed tool receipts before any tolerant SDK mirror."""
    if is_error is not False or type(structured) is not dict:
        return None
    raw = cast("dict[str, object]", structured)
    if not _same(_single_text(content), raw):
        return None
    if type(raw.get("session_id")) is not str or raw.get("session_id") != session_id:
        return None
    phase = raw.get("phase")
    if type(phase) is not str:
        return None
    if name in ("set_heat", "set_fan"):
        if raw.keys() != {
            "session_id",
            "phase",
            "heat_level_percent",
            "fan_level_percent",
            "cooling_on",
        } or phase not in ("pre_roast", "roasting"):
            return None
        for key in ("heat_level_percent", "fan_level_percent"):
            number = raw[key]
            if type(number) is not int or not 0 <= number <= 100:
                return None
        if type(raw["cooling_on"]) is not bool:
            return None
        key = "heat_level_percent" if name == "set_heat" else "fan_level_percent"
        return raw if type(value) is int and raw[key] == value else None
    if name != "mark_first_crack" or raw.keys() != {"session_id", "phase", "event", "event_count"}:
        return None
    if phase != "development" or type(raw["event_count"]) is not int:
        return None
    count = raw["event_count"]
    if not 1 <= count <= 2**53 - 1 or type(raw["event"]) is not dict:
        return None
    event = cast("dict[str, object]", raw["event"])
    if event.keys() != {"kind", "recorded_at_utc", "monotonic_seconds", "payload"}:
        return None
    seconds = event["monotonic_seconds"]
    if (
        type(event["kind"]) is not str
        or event["kind"] != "first_crack_detected"
        or type(event["recorded_at_utc"]) is not str
        or not event["recorded_at_utc"]
        or type(seconds) is not float
        or not math.isfinite(seconds)
        or seconds < 0
        or type(event["payload"]) is not dict
    ):
        return None
    return raw


@dataclass(frozen=True, slots=True)
class MarkReceipt:
    """Identity retained from a strictly validated EventCommandResult."""

    session_id: str
    monotonic_seconds: float
    event_count: int
    recorded_at_utc: str = ""
    payload: tuple[tuple[str, str | int | float | bool | None], ...] = ()


def pre_mark_evidence(record: TimingEvidence, binding: PrivateBinding) -> bool:
    """Require synthetic charge and absence of any existing FC or Drop."""
    return (
        record.qualification is Qualification.SYNTHETIC_MOCK_ONLY
        and record.child_epoch == binding.child_epoch
        and record.session_id == binding.session_id
        and record.phase == "roasting"
        and record.charge is not None
        and record.charge.clock_epoch_id == record.epoch_id
        and record.charge.time_basis is TimeBasis.PRODUCER_CANONICAL_RECORD
        and record.first_crack is None
        and record.drop is None
    )


def canonical_mark_matches(
    before: TimingEvidence,
    after: TimingEvidence,
    receipt: MarkReceipt,
    binding: PrivateBinding,
) -> bool:
    """Compare append identity, exact binary time, provenance and chronology."""
    fc = after.first_crack
    return (
        pre_mark_evidence(before, binding)
        and after.qualification is Qualification.SYNTHETIC_MOCK_ONLY
        and after.child_epoch == binding.child_epoch
        and receipt.session_id == after.session_id == binding.session_id
        and after.epoch_id == before.epoch_id
        and after.session_ownership_sequence == before.session_ownership_sequence
        and after.evidence_sequence > before.evidence_sequence
        and after.phase == "development"
        and after.charge == before.charge
        and after.drop is None
        and fc is not None
        and fc.clock_epoch_id == after.epoch_id
        and fc.source is EventSource.MANUAL_MARK
        and fc.time_basis is TimeBasis.PRODUCER_CANONICAL_RECORD
        and receipt.monotonic_seconds.hex() == fc.canonical_seconds.hex()
        and receipt.event_count - 1 == fc.event_index
    )
