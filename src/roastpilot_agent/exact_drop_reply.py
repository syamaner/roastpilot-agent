"""Closed receipts for the private synthetic exact Drop wire boundary."""

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import cast


class ExactDropDisposition(Enum):
    """Consumer dispositions granting no physical claim or retry right."""

    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    REFUSED = "refused"
    PRIOR_RECORDED = "prior_recorded"
    PRIOR_UNRESOLVED = "prior_unresolved"


class ExactDropReason(Enum):
    """Closed reasons retained only after complete tuple validation."""

    OBSERVATION_RESOLUTION_FAILED = "observation_resolution_failed"
    OBSERVATION_FREEZE_FAILED = "observation_freeze_failed"
    POST_DROP_CLEANUP_FAILED = "post_drop_cleanup_failed"
    MANUAL_MOCK_NORMAL_DROP_REQUIRED = "manual_mock_normal_drop_required"
    EXACT_BASELINE_UNAVAILABLE = "exact_baseline_unavailable"
    HUMAN_FIRST_CRACK_UNAVAILABLE = "human_first_crack_unavailable"
    EXACT_LIVE_OWNER_UNAVAILABLE = "exact_live_owner_unavailable"
    HUMAN_FIRST_CRACK_MISSING = "human_first_crack_missing"
    DISPATCH_RESERVATION_UNAVAILABLE = "dispatch_reservation_unavailable"
    TIMING_EVIDENCE_SUPERSEDED = "timing_evidence_superseded"
    ENDPOINT_ORIGIN_NOT_ESTABLISHED = "endpoint_origin_not_established"
    NORMAL_DROP_IN_PROGRESS = "normal_drop_in_progress"
    NORMAL_DROP_UNRESOLVED = "normal_drop_unresolved"


@dataclass(frozen=True, slots=True)
class ExactDropIdentity:
    """The four evaluated wire arguments, with no command authority."""

    session_id: str
    clock_epoch_id: str
    ownership_sequence: int
    timing_evidence_sequence: int

    def is_valid(self) -> bool:
        """Check exact scalar types and protected producer bounds."""
        return (
            type(self.session_id) is str
            and 1 <= len(self.session_id) <= 128
            and type(self.clock_epoch_id) is str
            and re.fullmatch(r"[0-9a-f]{32}", self.clock_epoch_id, re.ASCII) is not None
            and all(
                type(value) is int and 1 <= value <= 9_007_199_254_740_991
                for value in (self.ownership_sequence, self.timing_evidence_sequence)
            )
        )

    def arguments(self) -> dict[str, object]:
        """Return precisely the four named SDK arguments."""
        return dict(
            session_id=self.session_id,
            clock_epoch_id=self.clock_epoch_id,
            ownership_sequence=self.ownership_sequence,
            timing_evidence_sequence=self.timing_evidence_sequence,
        )


@dataclass(frozen=True, slots=True)
class ExactDropReceipt:
    """Validated producer facts; local uncertainty carries no received proof."""

    disposition: ExactDropDisposition
    identity: ExactDropIdentity | None = None
    reason: ExactDropReason | None = None
    observer_failure: bool | None = None
    cleanup_failure: bool | None = None


_TUPLE_KEYS = [
    "outcome",
    "reason",
    "request_dispatch",
    "this_request",
    "history",
    "fence",
    "prior_transition",
    "observer_failure",
    "cleanup_failure",
]
_KEYS = set(_TUPLE_KEYS) | {
    "session_id",
    "clock_epoch_id",
    "ownership_sequence",
    "timing_evidence_sequence",
    "retry",
    "physical_effect",
}
_SETTLED = ("transitioned", "reservation_owned", "saved", "released", "not_applicable")
_NON_ENTRY = ("not_invoked", "no_reservation", "not_applicable", "not_applicable", "not_applicable")
_ROWS: dict[tuple[object, ...], ExactDropDisposition] = {
    ("success", None, *_SETTLED, False, False): ExactDropDisposition.COMPLETED,
    ("success", "post_drop_cleanup_failed", *_SETTLED, False, True): ExactDropDisposition.COMPLETED,
    (
        "already_dropped",
        "endpoint_origin_not_established",
        *_NON_ENTRY,
        False,
        False,
    ): ExactDropDisposition.PRIOR_RECORDED,
    (
        "in_progress",
        "normal_drop_in_progress",
        *_NON_ENTRY,
        False,
        False,
    ): ExactDropDisposition.PRIOR_UNRESOLVED,
    (
        "ambiguous",
        "normal_drop_unresolved",
        "not_invoked",
        "no_reservation",
        "unknown",
        "retained",
        "unknown",
        False,
        False,
    ): ExactDropDisposition.PRIOR_UNRESOLVED,
}
for _reason in ("observation_resolution_failed", "observation_freeze_failed"):
    for _cleanup in (False, True):
        _ROWS[("success", _reason, *_SETTLED, True, _cleanup)] = ExactDropDisposition.COMPLETED
for _outcome, _reason in (
    ("refused_capability", "manual_mock_normal_drop_required"),
    ("refused_state", "exact_baseline_unavailable"),
    ("refused_precondition", "human_first_crack_unavailable"),
    ("refused_state", "exact_live_owner_unavailable"),
    ("refused_precondition", "human_first_crack_missing"),
    ("refused_state", "dispatch_reservation_unavailable"),
    ("refused", "timing_evidence_superseded"),
):
    _ROWS[(_outcome, _reason, *_NON_ENTRY, False, False)] = ExactDropDisposition.REFUSED


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_: str) -> object:
    raise ValueError


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or value.keys() != keys:
        raise ValueError
    return cast("dict[str, object]", value)


def decode_exact_drop_reply(
    is_error: object, content: object, structured: object, identity: ExactDropIdentity
) -> ExactDropReceipt:
    """Admit identical scalar mirrors matching a complete closed producer row."""
    unknown = ExactDropReceipt(ExactDropDisposition.UNKNOWN)
    try:
        if type(identity) is not ExactDropIdentity or not identity.is_valid():
            return unknown
        if is_error is not False or type(content) is not list:
            return unknown
        blocks = cast("list[object]", content)
        if len(blocks) != 1:
            return unknown
        block = _object(blocks[0], {"type", "text"})
        text = block["text"]
        if (
            type(block["type"]) is not str
            or block["type"] != "text"
            or type(text) is not str
            or len(text) > 16_384
        ):
            return unknown
        decoded: object = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        raw, mirror = _object(structured, _KEYS), _object(decoded, _KEYS)
        for key in _KEYS:
            if type(raw[key]) not in (str, int, bool, type(None)):
                return unknown
            if type(raw[key]) is not type(mirror[key]) or raw[key] != mirror[key]:
                return unknown
        for key, expected in identity.arguments().items():
            if type(raw[key]) is not type(expected) or raw[key] != expected:
                return unknown
        if raw["retry"] != "forbidden" or raw["physical_effect"] != "not_established":
            return unknown
        if any(type(raw[key]) is not bool for key in ("observer_failure", "cleanup_failure")):
            return unknown
        disposition = _ROWS.get(tuple(raw[key] for key in _TUPLE_KEYS))
        if disposition is None:
            return unknown
        reason = None if raw["reason"] is None else ExactDropReason(raw["reason"])
        return ExactDropReceipt(
            disposition,
            identity,
            reason,
            cast("bool", raw["observer_failure"]),
            cast("bool", raw["cleanup_failure"]),
        )
    except (ValueError, TypeError, OverflowError, RecursionError):
        return unknown
